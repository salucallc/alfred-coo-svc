# F. autonomous_build persona — implementation plan

**Date:** 2026-04-23
**Purpose:** Unblock v1.0 GA kickoff by adding `autonomous-build-a` persona as first-class COO daemon infrastructure. Pending kickoff mesh task `c609d3b3-8920-43ee-9640-0142024f87f1` will be claimable once this persona is live on Oracle.

## Summary of critical decisions + LoE

**LoE estimate: 28-39h wall-clock** with 2-3 parallel code sub-tasks (9h critical-path through scaffolding + orchestrator core; remaining items parallelizable).

**Critical decisions surfaced for Cristian's attention:**

1. **Break from one-shot model.** Current daemon `while True` loop in `main.py` claims → runs → completes → sleeps — this persona must claim-once and run for hours/days. Decision: spawn orchestrator as detached `asyncio.Task` on claim, so main poll loop keeps servicing other personas in parallel. Heartbeat extended to refresh liveness stamp inside long-running orchestrator task.
2. **State persists in soul memory + Linear, not Supabase.** Rather than new table, orchestrator writes structured `autonomous_build:<kickoff_task_id>:state` memory entry each cycle (wave index, per-ticket status map, cumulative cost). On daemon restart orchestrator recovers by reading memory. Re-uses existing infra; avoids schema change.
3. **Budget enforcement via token-counter aggregation, not billing stream.** No real-time HF/Ollama billing stream. Orchestrator approximates: reads child mesh-task `result.tokens.{in,out}` × price table (reuse `pricing.yaml` from OPS-23 once landed; until then hardcode dict from Ollama Max + HF rate card). Hard-stop at `$30`, Slack warn at `$20`.
4. **SS-08 gate uses Slack reply-polling, not reaction watcher.** Reactions require Slack events API / socket mode — daemon doesn't run that. Instead: emit JWS claims schema to #batcave, poll `conversations.history` for Cristian's reply containing `ACK` / `approve` keywords. New tool `slack_ack_poll`.
5. **Cross-node claim race punt.** Current claim mutation is atomic at DB layer (409 on dup) — losing daemon goes idle. Acceptable for v1. Re-entrancy on restart handled by (2).

---

## 1. Architecture

### Repo plug-in points (evidence-based from cloned `salucallc/alfred-coo-svc`)

- **Registration:** `src/alfred_coo/persona.py` `BUILTIN_PERSONAS` dict — add `autonomous-build-a` entry. Persona dataclass already supports `tools`, `preferred_model`, `fallback_model`, `topics`. One new field: `handler: Optional[str]` naming a long-running orchestrator class (default `None`). Clean extension point — most personas stay "one-shot", only this one opts into new flow.
- **Tag parser:** `src/alfred_coo/mesh.py::parse_persona_tag()` already handles `[persona:<name>]`. No change — kickoff title will be `[persona:autonomous-build-a] Mission Control v1.0 GA — kickoff`.
- **Claim/dispatch fork:** `src/alfred_coo/main.py` ~L98-150. After claim, inspect `persona.handler`. If `None`, current one-shot. If set, instantiate `AutonomousBuildOrchestrator(task, soul, mesh, dispatcher, settings)` and `asyncio.create_task(orch.run())`. Do NOT `await` — register in `_running_orchestrators: dict[task_id, asyncio.Task]` so main loop continues.
- **One-shot contract preserved** for every other persona. Load-bearing: `hawkman-qa-a`, `riddler-crypto-a`, `batgirl-sec-a`, `alfred-coo-a` unchanged.

### Lifecycle

```
[poll loop] → claim kickoff c609d3b3... → spawn AutonomousBuildOrchestrator
    │                                              │
    │ continues polling (other personas)           │ resume-from-memory if restart
    │                                              │ parse kickoff payload (§5 JSON)
    │                                              │ load Linear tickets (GraphQL)
    │                                              │ build wave/dep graph
    │                                              │ FOR wave in [0,1,2,3]:
    │                                              │   dispatch_wave(wave)
    │                                              │   poll_child_status()
    │                                              │   wait_for_wave_gate()
    │                                              │ on_all_green → tag + notify
    │                                              │ mesh.complete(kickoff)
    ↓                                              ↓
[heartbeat]  ← orchestrator stamps liveness every 60s, main-loop every 30s
```

Heartbeat decoupling: `main.py` already calls `mesh.heartbeat(...)` each poll. Add second caller inside `AutonomousBuildOrchestrator._status_tick()` so long-running work surfaces current-task string reflecting wave progress ("wave 1 / 18-of-22 green / $4.20 spent").

### Data flow

```
Linear GraphQL (issues + BLOCKS relations)
        │
        ▼
ticket graph + wave labels
        │
        ▼
WaveDispatcher.dispatch(wave_n)
        │  (per ticket → [persona:alfred-coo-a] child mesh task)
        ▼
mesh_task_create (existing tool, re-used internally)
        │
        ▼
polls /v1/mesh/tasks?status=completed&limit=50 every 45s
        │
        ▼
match child.result → ticket_status[id]
        │                                          │
        ▼                                          ▼
Linear update (Backlog→Todo→In Progress→Done)  Slack 20-min cadence
                                                   │
                                                   ▼  on blocker >30min on critical-path
                                               Slack critical-path ping
```

---

## 2. Interfaces

### Input contract
JSON payload in kickoff task `description`, exactly as spec'd in `HANDOFF_V1_GA_MASTER_2026-04-23.md` §5: `task_type, project, linear_project_id, ticket_range, concurrency, model_routing, verification, budget, status_cadence, wave_order, wave_gate, on_all_green`. Orchestrator validates on startup; mismatched/unknown keys log-and-continue (forward compat).

### Child task shape (example for SAL-2583 / TIR-01)

Title: `[persona:alfred-coo-a] [wave-0] [tiresias] SAL-2583 TIR-01 — tiresias-sovereign repo scaffold`

Description:
```
Ticket: SAL-2583 (TIR-01)
Linear: https://linear.app/saluca/issue/SAL-2583
Wave: 0
Epic: Tiresias
Size: M (estimated 6-8h)
Model: qwen3-coder:480b-cloud (from kickoff.model_routing.code_heavy_large)
Parent autonomous_build: <kickoff_task_id>

## Acceptance (from plan A_tiresias_in_appliance.md §5)
- [paste APE/V acceptance block for TIR-01]

## Plan doc context
Z:/_planning/v1-ga/A_tiresias_in_appliance.md

## Deliverable
Open PR to salucallc/<target-repo> on branch feature/SAL-2583-tiresias-scaffold.
```

`alfred-coo-a` claims → builds → `propose_pr` → returns PR url. Orchestrator cross-references PR via `pr_files_get` / `http_get` GitHub API; merged-green → Done; otherwise fans out `hawkman-qa-a` review task.

### Status model

```python
TicketStatus = Literal[
    "pending",         # not yet dispatched
    "dispatched",      # mesh_task_create called
    "in_progress",     # child claimed
    "pr_open",         # child completed with PR URL, not merged
    "reviewing",       # hawkman-qa-a review in flight
    "merge_requested", # review APPROVE + no blockers
    "merged_green",    # PR merged + CI green
    "failed",          # max retries exhausted or hard error
    "blocked",         # waiting on dependency
]
```

Linear transitions: `Backlog` (pending) → `Todo` (dispatched) → `In Progress` (in_progress/pr_open/reviewing) → `Done` (merged_green) via `linear_update_issue_state(id, state_name)` helper in `tools.py`.

---

## 3. Wave-gate logic

### Algorithm

```python
async def run_program():
    graph = await build_ticket_graph(linear_project_id)   # nodes=tickets, edges=BLOCKS
    for wave in [0, 1, 2, 3]:
        in_flight: set[str] = set()
        wave_tickets = [t for t in graph if t.wave == wave]
        while not all_done(wave_tickets):
            ready = [t for t in wave_tickets
                     if t.status == "pending"
                     and all(dep.status == "merged_green" for dep in t.blocks_in)
                     and count_in_epic(t.epic, in_flight) < 3
                     and len(in_flight) < 6]
            for t in ready:
                await dispatch_child(t)
                in_flight.add(t.id)
            updates = await poll_children(in_flight)
            in_flight -= {t for t in updates if is_terminal(t.status)}
            await maybe_slack_tick()
            await maybe_budget_check()
            await asyncio.sleep(45)
        assert all(t.status == "merged_green" for t in wave_tickets), "wave gate failed"
```

### Wave-internal dependencies
BLOCKS edges respected regardless of wave — `all(dep.status == "merged_green" for dep in t.blocks_in)` handles uniformly. OPS-04 → OPS-05 → OPS-06 serial chain flows naturally.

### Failure modes

| Failure | Detection | Response |
|---|---|---|
| Child stuck >2h no completion | `updated_at` stale | Respawn with constrained <300-char prompt (hawkman-qa-a fallback pattern) |
| PR REQUEST_CHANGES loop (>3 cycles) | count on same PR | Escalate to Slack, mark `failed`, do NOT advance wave |
| Silent-complete (empty summary) | post-complete inspection | Respawn with progress_checker constrained prompt |
| Hard error (Linear 5xx, Supabase down) | exception in loop | try/except around loop body; Slack warn; sleep 60s; continue |
| Budget burn to 80% | cumulative/30 > 0.8 | Slack warn, continue |
| Budget burn to 100% | hard | Stop new dispatches. Let in-flight complete. Slack hard-stop. Mesh task → `failed` with state dump. |

---

## 4. Budget + cadence

### Budget enforcement
- **Source:** read each completed child's `result.tokens.{in,out}` and `result.model`, apply price-per-MTok dict, accumulate.
- **Location:** `autonomous_build/budget.py` — in-memory rolling total; persisted to soul memory every tick.
- **Hard stop:** at `$30` (kickoff payload). No new `mesh_task_create`. Post Slack "BUDGET HARD STOP $30.01 reached, <N> in-flight, <M> dispatched, <K> green" then drain-mode.
- **Channel:** C0ASAKFTR1C (#batcave).
- **Pricing table:** hardcode in `budget.py::PRICE_PER_MTOK` keyed by model string. Once OPS-23 (`pricing.yaml`) lands in Wave 1, swap to reading that file.

### Slack cadence (20-min timer)

```
Mission Control v1.0 GA — autonomous_build tick HH:MM
Wave 1 (18/22 green, 3 in-flight, 1 pending)
$4.20 / $30 spent
Wave 0: 15/15
Critical path: OPS-04 → OPS-05 → OPS-06 in progress (alfred-coo-a claimed 00:12 ago)
```

### Critical-path ping (event-driven)
Orchestrator tags every ticket with `is_critical_path` from `critical-path` Linear label. If critical-path ticket's `in_progress`/`reviewing` elapses >30 min no transition, post separate Slack: `Critical-path block: SAL-XXXX YYY stalled 34m in reviewing, last: hawkman REQUEST_CHANGES #3`.

---

## 5. SS-08 gate handling

SS-08 = SAL-SS-08 (PQ receipt endpoint). Plan E §5.1: acceptance = JWS-signed receipt `{receipt, pubkey_sha, signed_at}`.

**Gate flow:**
1. Orchestrator about to dispatch SAL-SS-08.
2. Checks `if ticket.code == "SS-08" and not self._ss08_acked:`
3. Reads JWS claims schema from `E_soul_svc_gaps.md` §5 via `http_get` or embedded constant.
4. Posts #batcave:
   ```
   GATE: SS-08 (PQ receipt) — JWS claims schema pending Cristian sign-off.
   Reply with `ACK SS-08` or `approve SS-08` to proceed.
   Schema:
   <embed claims schema YAML>
   ```
5. Polls `conversations.history` every 2 min via new tool `slack_ack_poll(channel, after_ts, author_user_id, keywords)`.
6. Author user_id: hardcoded `CRISTIAN_SLACK_USER_ID = "U0AH88KHZ4H"` (bot lacks `users:read.email` scope; Slack app reinstall declined 2026-04-23).
7. Match: Cristian's message after `ts`, text `(?i)(ack|approve(d)?)\s*ss[-_ ]?08`.
8. On ACK: `self._ss08_acked = True`, dispatch child, continue. On 4h timeout: skip + mark deferred to v1.1 per D2 recommendation.

**Required new Slack scopes:** `channels:history`, `users:read.email`. Ticket as AB-03.

---

## 6. Testing plan

### Unit tests (`tests/test_autonomous_build.py`)
- `test_wave_gate_blocks_until_all_green` — seed graph, 9/10 green, assert wave N+1 dispatch = 0.
- `test_dependency_respect_within_wave` — B blocks A both wave 1; B not dispatched until A.merged_green.
- `test_budget_hard_stop_halts_new_dispatch` — spend=$29.50 + completion → $30.50, no new `mesh_task_create`.
- `test_ss08_gate_blocks_until_ack` — halts at SS-08; mock Slack returns ACK on 3rd poll; assert dispatch proceeds.
- `test_restart_recovery_reads_memory` — write state memory, fresh orchestrator reconstructs wave+ticket_status.
- `test_per_epic_cap_enforced` — 5 Tiresias tickets ready, cap=3, only 3 concurrent.

### Integration test (dry-run)
Env `AUTONOMOUS_BUILD_DRY_RUN=1`. Stubs `mesh_task_create` / `linear_update_issue` / `slack_post`. 3-ticket mock project (wave 0 × 2, wave 1 × 1), assert wave 0→1 transition and right Slack transcript. <10s CI.

### Smoke test (live, narrow)
Throwaway Linear sub-project "autonomous-build-smoke" with 3 real tickets. Kickoff with `ticket_whitelist`. Observe wave-0 dispatch, completion, Slack cadence. Cost cap: $1.00. Runs AFTER PR merge, BEFORE the real 95-ticket kickoff.

---

## 7. Implementation tickets (7 sub-tickets)

| # | Title | Deliverable | Size | Deps |
|---|---|---|---|---|
| AB-01 | feat(persona): scaffold `autonomous-build-a` registration + handler field | `persona.py` gains `handler` field; `autonomous-build-a` entry added; unit test green | S (2-3h) | — |
| AB-02 | feat(main): long-running orchestrator spawn hook, non-blocking poll | `main.py` detects `persona.handler`, creates asyncio.Task, tracks `_running_orchestrators` | M (4-6h) | AB-01 |
| AB-03 | feat(tools): `slack_ack_poll`, `linear_update_issue_state`, `linear_list_project_issues`, `linear_get_issue_relations` | 4 new tools in `tools.py`; registered in `BUILTIN_TOOLS`; unit tested; Slack app scopes updated | M (5-7h) | AB-01 |
| AB-04 | feat(autonomous_build): orchestrator core — ticket graph, wave scheduler, dep resolver | `alfred_coo/autonomous_build/{__init__.py, orchestrator.py, graph.py, state.py}`; unit tests wave gate + dep resolution | L (8-10h) | AB-02, AB-03 |
| AB-05 | feat(autonomous_build): budget tracker, Slack cadence tick, critical-path ping | `budget.py`, `cadence.py`; integration with orchestrator; budget hard-stop tests | M (4-6h) | AB-04 |
| AB-06 | feat(autonomous_build): SS-08 gate + ACK polling | Gate logic + `slack_ack_poll` wiring; mocked Slack unit test; README snippet | S (2-3h) | AB-03, AB-04 |
| AB-07 | feat(autonomous_build): dry-run mode + smoke test harness | `AUTONOMOUS_BUILD_DRY_RUN` env; 3-ticket mock graph runner; CI workflow addition | S (3-4h) | AB-05, AB-06 |

**Total: ~28-39h.** AB-01/02/03 land first → parallel work on AB-04/05/06. Single builder sub picks AB-04 (L) while another handles AB-05+AB-06 (M+S); AB-07 (smoke) last.

---

## 8. Rollout plan

1. Open PR on `salucallc/alfred-coo-svc` with AB-01..AB-07 (stacked, merge in order).
2. Existing CI (`.github/workflows/publish.yml`) builds multi-arch, pushes `ghcr.io/salucallc/alfred-coo-svc:latest` + sha-prefixed tag. No CI change.
3. On Oracle: `docker pull ghcr.io/salucallc/alfred-coo-svc:latest && systemctl restart alfred-coo` (or compose variant per `deploy/alfred-coo.service`).
4. Daemon poll loop sees pending kickoff `c609d3b3...`, matches `[persona:autonomous-build-a]`, claims, spawns orchestrator.
5. Orchestrator begins wave 0 dispatch. First Slack tick within 20 min confirms liveness.
6. Rollback: `docker tag ghcr.io/salucallc/alfred-coo-svc:sha-6916335 ghcr.io/salucallc/alfred-coo-svc:latest && systemctl restart alfred-coo`. v1.0.0-beta SHA `6916335ed7086cf8ea02b9723307f21f242c9985` stays pinned as known-good fallback. State memory persists — re-rolled-forward image resumes from last checkpoint.

---

## 9. Risks + open questions

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | Cross-node claim race (Oracle daemon + local dev instance) | Low | Supabase claim atomic (409 on dup); losing claimant idle. Acceptable v1. |
| R2 | Orchestrator crash mid-dispatch; child orphan | Med | State checkpoint to soul memory every tick. On restart orchestrator reconciles orphans by querying mesh_tasks with parent tag. |
| R3 | Budget hits while 6 children mid-PR | Med | Locked: **let in-flight drain, block new dispatch**. Kicked-off PRs cheap to finish; prevent new burn. Slack "drain mode" notice. |
| R4 | Long-running claim + daemon restart = task looks stuck `claimed` | Med | Heartbeat from orchestrator in same session; mesh heartbeat fresh across restarts (state recovered from memory). If cold >10 min, operator force-reclaim runbook (deferred to AB-07 docs). |
| R5 | Token counter underestimates vs real Ollama/HF billing | Med | Document assumption; `cost_confidence: estimate` label in Slack. Swap when OPS-23 pricing.yaml lands. |
| R6 | Slack ACK polling misses ACK in busy channel (scrolled past) | Low | `after_ts` monotonic from gate post, `limit=100`, paginate on cursor. Coverage in AB-06. |

### Open questions for Cristian (before AB-01 merge)

- **Q1.** Is $30 hard stop truly hard, or "soft warn, continue if under $50"? Kickoff says hard; confirm.
- **Q2.** Orchestrator opens Linear PRs under its own bot identity, or route through `alfred-coo-a`? **Recommend: route through child** — keeps orchestrator pure controller, zero new GitHub auth.
- **Q3.** SS-08 4h ACK timeout: skip + mark deferred, or halt entire program? **Recommend: skip + defer** (D2 recommendation in handoff §4 E2).
- **Q4.** `cristian@saluca.com` the right Slack lookup target? Context confirms yes.

---

## Critical files for implementation
- `src/alfred_coo/main.py` — poll loop; orchestrator spawn hook
- `src/alfred_coo/persona.py` — registry + new `handler` field + `autonomous-build-a` entry
- `src/alfred_coo/tools.py` — new slack_ack_poll, linear_update_issue_state, linear_list_project_issues, linear_get_issue_relations
- `src/alfred_coo/mesh.py` — existing claim/complete/heartbeat client, re-used verbatim
- `Z:/_planning/v1-ga/HANDOFF_V1_GA_MASTER_2026-04-23.md` §5 — kickoff payload = input contract
