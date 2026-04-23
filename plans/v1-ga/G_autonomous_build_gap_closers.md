# G. Autonomous Build — Gap Closers (AB-08, AB-09, AB-10)

Two (optionally three) follow-ups to the AB-01..AB-07 persona that surfaced on the first live Oracle run (2026-04-23, kickoff task `3511ee55`, image SHA `e163daf3` after PR #26).

**Total LoE: 10-15h**, 1 wall-day with 2 parallel builders (AB-10 unblocks AB-08).

| # | Title | Size | Deps |
|---|---|---|---|
| AB-10 | feat(tools): `github_merge_pr` helper + registration | S (2-3h) | — |
| AB-08 | feat(autonomous_build): REVIEWING → MERGED_GREEN completion loop | M (5-7h) | AB-10 |
| AB-09 | feat(autonomous_build): zombie-orchestrator guard + kickoff claim refresh | M (4-5h) | — |

## Locked decisions (2026-04-23)

- **Merge strategy**: `squash` (matches Saluca convention observed throughout this session's PRs)
- **AB-10 as separate ticket**: yes — cleaner mock boundary for AB-08 tests
- **G3**: soul-svc claim idempotency verified empirically (see §AB-09 Layer 1)

---

## AB-08 — Review completion loop

### Problem

`orchestrator._poll_children` (orchestrator.py:605-698) transitions `PR_OPEN → REVIEWING` by firing `_dispatch_review` (orchestrator.py:744-771), but **no code reads the review task's result back**. Tickets rot in `REVIEWING`; PRs never merge; the wave gate can never go all-green. Terminal states are only `MERGED_GREEN` or `FAILED` (graph.py:44-45) — any ticket stuck in `REVIEWING` forever keeps `_wait_for_wave_gate` spinning.

### Design

#### 1. State additions

Add three fields to `OrchestratorState` (state.py:33-58) — all forward-compatible (`from_json` drops unknown keys, so old snapshots keep loading):

```python
review_task_ids: Dict[str, str]       # ticket_uuid → hawkman mesh task id
review_verdicts: Dict[str, str]       # ticket_uuid → last verdict
merged_pr_urls: Dict[str, str]        # ticket_uuid → merge_commit_sha or pr_url
```

Add one field to `Ticket` (graph.py:67-95):

```python
review_task_id: Optional[str] = None
```

Mirror in `_apply_restored_status` + `_snapshot_graph_into_state` (orchestrator.py:340-386).

#### 2. `_dispatch_review` captures the task id

Currently `_dispatch_review` discards the mesh response. Change to stash `resp["id"]` on `ticket.review_task_id` **before** it marks the ticket `REVIEWING`. This is the seed data the new poller uses.

#### 3. New `_poll_reviews()` sibling to `_poll_children`

Called from `_dispatch_wave` inner loop **after** `_poll_children` and **before** `_check_budget`. Flow per `REVIEWING` ticket:

```
for ticket in graph where status == REVIEWING and review_task_id is set:
    rec = completed_by_id.get(ticket.review_task_id)       # reuse the list_tasks call
    if rec is None:
        continue                                            # still in flight
    verdict = _extract_verdict(rec.get("result") or {})
    if verdict == "APPROVE":
        ticket.status = MERGE_REQUESTED
        merged = await _merge_pr(ticket)
        if merged:
            ticket.status = MERGED_GREEN
            await _update_linear_state(ticket, "Done")
            record_event("ticket_merged", ...)
        else:
            ticket.status = FAILED
            record_event("ticket_merge_failed", ...)
    elif verdict == "REQUEST_CHANGES":
        if ticket.review_cycles >= MAX_REVIEW_CYCLES:       # 3
            ticket.status = FAILED
            record_event("review_max_cycles", ...)
        else:
            await _respawn_child_with_fixes(ticket, review_body)
            ticket.status = DISPATCHED
    elif verdict == "COMMENTED_FALLBACK":
        parsed = _parse_fallback_verdict(rec)
        # recurse with parsed verdict, or FAILED if ambiguous
    else:
        # Silent/missing. Retry once by redispatching review.
        ticket.silent_review_retries += 1
        if ticket.silent_review_retries > 1:
            ticket.status = FAILED
        else:
            await _dispatch_review(ticket)
```

Share the `by_id` dict with `_poll_children` (stash on `self._last_completed_by_id`) to avoid a second `mesh.list_tasks` call.

#### 4. `_extract_verdict(result)` helper

Walks `result` like `_extract_pr_url`. Priority:

1. `result.tool_calls[*].result.state` where tool was `pr_review` (values: `APPROVE`, `REQUEST_CHANGES`, `COMMENT`, `COMMENTED_FALLBACK`)
2. `result.summary` regex for uppercase `APPROVE` / `REQUEST_CHANGES`
3. `result.follow_up_tasks` string scan

Return `None` for silent.

#### 5. `_parse_fallback_verdict(rec)` helper

Trusts `tool_calls[].result.intended_event` from `pr_review` fallback return (tools.py:509 already populates it). No text parsing needed. If absent → treat as silent.

#### 6. `_merge_pr(ticket)` delegates to AB-10

Parses `owner/repo/pr_number` from `ticket.pr_url` (regex on `_PR_URL_RE`, already at orchestrator.py:45). Calls `github_merge_pr(owner, repo, pr_number, merge_method="squash")`. On non-success → False → caller marks FAILED.

#### 7. `_respawn_child_with_fixes(ticket, review_body)`

New alfred-coo-a mesh task:
- Title: `[persona:alfred-coo-a] [wave-N] [epic] SAL-XXX — fix: round {n}`
- Body: original APE/V + review comments excerpt + explicit "push to existing branch; do NOT open a new PR"
- Increments `ticket.review_cycles`
- Sets `ticket.child_task_id = new_id`, status `DISPATCHED`

### Safety / correctness

- **Cycle cap (3)**: constant `MAX_REVIEW_CYCLES = 3`. Critical-path tickets that blow the cap halt the wave via existing logic.
- **Double-merge guard**: before `_merge_pr`, check `ticket.status != MERGED_GREEN and ticket.id not in state.merged_pr_urls`.
- **Idempotency on restart**: ticket in `MERGE_REQUESTED` on resume → re-fire merge; GitHub returns 405 "already merged" which we treat as success.
- **Linear mirror**: APPROVE→merge → Linear `Done`. FAILED → existing Backlog/Canceled logic.

### Tests (tests/test_autonomous_build_review_loop.py)

1. Approve happy path → MERGED_GREEN + Linear Done
2. Request changes → new child spawned, review_cycles=2
3. Max cycles → FAILED
4. Fallback APPROVE (intended_event) → treated as APPROVE
5. Silent review → retries=1 triggers second review dispatch
6. Silent review 2nd time → FAILED
7. Merge failure → FAILED not MERGED_GREEN
8. Restart resumes review_task_ids mapping

### Size

**M (5-7h).**

---

## AB-10 — `github_merge_pr` tool (recommended)

### Why carve this out

`tools.py` has `pr_review` + `pr_files_get` + `propose_pr` but no merge. Inlining in `orchestrator._merge_pr` would duplicate GITHUB_TOKEN / org-allowlist / 422-fallback patterns from `pr_review`. Separate tool:

- Reusable (future operator/persona calls)
- Clean mock surface for AB-08 tests
- One place for merge failure handling

### Design

```python
async def github_merge_pr(
    owner: str,
    repo: str,
    pr_number: int,
    merge_method: str = "squash",    # squash | merge | rebase
    commit_title: Optional[str] = None,
    commit_message: Optional[str] = None,
) -> Dict[str, Any]:
    """PUT /repos/{owner}/{repo}/pulls/{pr_number}/merge
    Returns {ok, merged, sha, message} or {error, response}."""
```

- Reuses `_ALLOWED_ORGS`, `_VALID_OWNER_RE`, `_VALID_REPO_RE` from tools.py
- `GITHUB_TOKEN` env
- Error mapping: 405 → `not_mergeable`; 409 → `stale_head`; 422 → return as-is
- No 422-self-authored fallback (merging own PR is allowed)
- Register in `BUILTIN_TOOLS`

### Tests

- Missing token → error
- Bad owner → allowlist error
- Success (mocked urlopen) → `{ok, sha}`
- 405 → `not_mergeable`

### Size

**S (2-3h).**

---

## AB-09 — Zombie orchestrator guard

### Problem

On 2026-04-23 a kickoff mesh task was claimed, daemon restarted, soul-svc claim TTL expired the claim back to `pending`, daemon re-claimed on restart, spawned a second orchestrator for the same `linear_project_id`. Two orchestrators → double-dispatch children, race on soul memory writes, thrash Linear.

### Design — two layers

#### Layer 1: Kickoff claim refresh (DEFERRED — soul-svc lacks idempotent endpoint)

**G3 verification 2026-04-23: soul-svc claim endpoint returns 409 "Task already claimed or does not exist" when called as the current holder.** The endpoint does NOT support idempotent re-claim-as-heartbeat. Layer 1 as originally designed would generate a flood of 409s and never refresh the claim.

**Path forward (follow-up, not in AB-09)**: add new endpoint `PATCH /v1/mesh/tasks/{id}/heartbeat` to soul-svc (requires salucallc/soul-svc PR) that accepts `{session_id, node_id}` and if they match the holder, bumps `claimed_at` without changing ownership. File as **AB-11 (S, ~2h on soul-svc side + ~1h integration)**.

For v1, Layer 1 ships as: document the zombie risk in README; operator best practice is to avoid daemon restarts during active autonomous_build; if a restart is needed, manually mark the claimed kickoff task as failed before restart (the orchestrator's current `_fail_kickoff` finally path does this on graceful shutdown; only hard-kills leak).

#### Layer 2: Spawn-time duplicate guard (local daemon)

`main.py:38` has `_running_orchestrators: dict[str, asyncio.Task]` keyed by mesh task id. Different mesh task ids for the same Linear project can still spawn duplicates. Add parallel index:

```python
_orchestrators_by_project: dict[str, str] = {}   # linear_project_id → mesh_task_id
```

In `_spawn_long_running_handler` before creating the orchestrator class:

1. Parse `task["description"]` as JSON, peek `linear_project_id` (refactor into `_peek_kickoff_project_id(task)` helper — share with orchestrator.py)
2. Look up `_orchestrators_by_project.get(project_id)`
3. If found AND stashed handle not done: duplicate. Call `mesh.complete(task["id"], status="failed", result={"error": f"duplicate_kickoff: existing orchestrator task={existing_id} running for project={project_id}"})` and return False without spawning
4. Else: spawn. On success: `_orchestrators_by_project[project_id] = task["id"]`
5. On orchestrator completion (add `done_callback` to asyncio.Task): pop the project entry

#### Layer 3: Cross-daemon awareness (documented v1 limitation)

Single Oracle daemon is the only autonomous_build runner (ops contract). Zombie scenario reproduces only on single-daemon restart — Layer 1 solves it fully. README notes concurrent multi-daemon unsupported.

### Tests (tests/test_main_orchestrator_spawn.py — extend)

1. Duplicate kickoff same project → 2nd marked failed with `duplicate_kickoff`
2. Different projects allowed → both live
3. Completion clears the slot → second spawn of same project succeeds
4. Kickoff refresh fires → `claim` called ≥2× with kickoff id
5. Refresh cancels on completion → `.cancelled() is True`

### Size

**M (4-5h).**

---

## Risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| G1 | AB-08 merge fires on broken PR (hawkman approved despite failing CI) | Med | Hawkman prompt requires green CI as prerequisite. Follow-up: orchestrator calls `github_get_pr_status` before merge; defer as AB-11 unless G1 hits. |
| G2 | AB-08 review-cycle infinite loop | Low | Hard cap 3 → FAILED. Wave gate handles. |
| G3 | Layer-1 re-claim collides with soul-svc | **Verified 2026-04-23: IDEMPOTENT for same holder** | — |
| G4 | AB-10 wrong merge strategy | Low | squash default matches convention; override via payload |
| G5 | Self-authored PR merge despite COMMENTED_FALLBACK | Med | Trust `intended_event`. Follow-up: identity split (D ops). |

## Follow-up items (out of scope)

- **Split identity**: reviewer token ≠ builder token so `pr_review` never hits 422 fallback (ops layer D)
- **`github_get_pr_status` tool**: gate merge on green CI. Carve as **AB-11 (S)** if G1 materialises.
- **Per-project advisory lock in soul memory**: cross-daemon coordination. **AB-12 (M)** if fleet mode introduces multi-node daemons.

## Rollout

1. Merge AB-10 first (no behavioural change on its own)
2. Merge AB-09 next (safe in isolation)
3. Merge AB-08 last (depends on AB-10). Verify via AUTONOMOUS_BUILD_DRY_RUN=1 smoke before Oracle
4. Oracle: `docker pull && systemctl restart alfred-coo`. Active orchestrator task `3511ee55` resumes from soul checkpoint with new review logic active

## Critical files

- `src/alfred_coo/autonomous_build/orchestrator.py` — `_poll_reviews`, `_dispatch_review` hookup, `_merge_pr`, kickoff refresh task
- `src/alfred_coo/autonomous_build/state.py` — new review_task_ids/review_verdicts/merged_pr_urls
- `src/alfred_coo/autonomous_build/graph.py` — `Ticket.review_task_id`
- `src/alfred_coo/tools.py` — `github_merge_pr` + BUILTIN_TOOLS registration
- `src/alfred_coo/main.py` — `_orchestrators_by_project` + `_peek_kickoff_project_id`
