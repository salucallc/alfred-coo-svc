# AD-e: Action layer (Tier-1 auto, Tier-2 Slack)

**Parent:** alfred-doctor epic — see `plans/v1-ga/AD.md`
**Linear:** [SAL-3285](https://linear.app/saluca/issue/SAL-3285)
**Wave:** wave-2

## Context

Step 5 of the architectural skeleton. Two-tier execution helper that routes proposed actions either to direct execution (Tier-1: safe) or to Slack approval (Tier-2: risky). Independent of the investigation loop so it can be unit-tested without LLM calls. AD-d and AD-f hand `Action` records to this layer.

Tier-1 auto-execute: memory write, Linear comment, dashboard alert, decomp ticket suggestion (creates a Linear ticket but does NOT close any existing one).

Tier-2 Slack approval: registry edits, daemon restart, code PRs, ticket label changes, ticket state changes.

## Target paths

* `src/alfred_doctor/actions.py`
* `src/alfred_doctor/action_log.py`
* `tests/test_actions.py`
* `plans/v1-ga/AD-e.md`

## Dependencies

Upstream: AD-a (writes to `state.db` for memory writes), AD-c (Slack helper for tier-2).
Downstream: AD-d (calls Tier-1 to record findings), AD-f (calls Tier-1 + Tier-2 based on tree resolutions).

## APE/V Acceptance

**A — Action:**

1. Add `src/alfred_doctor/actions.py` with class `ActionRouter` exposing `ActionRouter.execute(action: Action) -> ActionResult`. The router inspects `action.kind` and dispatches:
   * Tier-1 kinds (auto): `memory_write`, `linear_comment`, `dashboard_alert`, `decomp_ticket_suggest` → execute immediately.
   * Tier-2 kinds (Slack-gated): `registry_edit`, `daemon_restart`, `code_pr`, `ticket_label_change`, `ticket_state_change` → call `slack_io.post_for_approval`, then `slack_io.read_approval`, then execute or skip.
2. Add `src/alfred_doctor/action_log.py` with `log_action(action, result)` that appends a JSON line to `/var/log/alfred-doctor/actions.log` with `timestamp`, `kind`, `tier`, `result`, `reasoning_ref`.
3. `Action` and `ActionResult` are dataclasses with mandatory fields: `kind`, `payload`, `reasoning`, plus result-side `status`, `details`, `slack_thread_ts` (optional).
4. Add `tests/test_actions.py` with at least 5 cases: (a) Tier-1 memory_write succeeds without Slack call, (b) Tier-2 registry_edit posts Slack and waits for approval, (c) Tier-2 with `deny` reply skips execution and logs `status="denied"`, (d) Tier-2 timeout logs `status="timeout"`, (e) every executed action lands as a row in `actions.log`.

**P — Plan:**

* `actions.log` directory created lazily (`os.makedirs(..., exist_ok=True)`).
* Tier-1 memory_write goes through the existing soul-svc client.
* Tier-2 dispatcher uses `slack_io.post_for_approval` and `slack_io.read_approval` from AD-c.
* The router never executes a Tier-2 action without a thread `ts` (sanity invariant).

**E — Evidence:**

* `git diff` showing the four new files.
* `pytest tests/test_actions.py -v` output: 5+ tests, all green.
* A sample `actions.log` excerpt from a test run pasted into the PR description.

**V — Verification (machine-checkable):**

1. File `src/alfred_doctor/actions.py` exists with class `ActionRouter` and method `execute`.
2. Action `kind` values are partitioned into exactly two tiers; the partitioning is verified by a parametrized test that asserts no kind appears in both tiers.
3. `pytest tests/test_actions.py -v` exits 0 with at least 5 test cases covering the matrix above.
4. Tests assert that every `ActionRouter.execute` call (whether executed, denied, or timed out) appends a JSON-line row to `actions.log`.
5. Plan doc `plans/v1-ga/AD-e.md` exists with an APE/V Acceptance section byte-identical to this Linear description's.
