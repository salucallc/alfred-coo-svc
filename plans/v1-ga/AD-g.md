# AD-g: Learning subsystem (runbook table + weekly summary)

**Parent:** alfred-doctor epic — see `plans/v1-ga/AD.md`
**Linear:** [SAL-3287](https://linear.app/saluca/issue/SAL-3287)
**Wave:** wave-4

## Context

Step 6 of the architectural skeleton. Each surveillance + investigation outcome lands in `state.db`. Patterns that prove out (verified-true root cause) get codified into the `runbook` table with their `severity_weight` bumped up. Patterns that prove false get demoted (severity_weight decayed). Weekly self-summary posted to `#batcave` summarising what alfred-doctor learned that week.

This is the loop that lets alfred-doctor get smarter over time without code changes; the surveillance loop reads `runbook.weight` to bias scoring.

## Target paths

* `src/alfred_doctor/learning.py`
* `src/alfred_doctor/weekly_summary.py`
* `tests/test_learning.py`
* `plans/v1-ga/AD-g.md`

## Dependencies

Upstream: AD-a (writes `runbook` rows), AD-d (consumes `InvestigationResult`), AD-f (consumes `RootCauseResult`), AD-c (Slack helper for summary).
Downstream: AD-b (reads `runbook.weight` once this lands).

## APE/V Acceptance

**A — Action:**

1. Add `src/alfred_doctor/learning.py` with class `Learner` exposing:
   * `record(survey, investigation, root_cause)`: writes a row into `state.db.outcomes` and updates `runbook` weights.
   * `update_weights(outcome)`: bumps weight by `+0.2` on verified-true, decays by `*0.9` on verified-false, leaves unchanged on inconclusive. Weight is clamped to `[0.1, 5.0]`.
2. Add `src/alfred_doctor/weekly_summary.py` with `compose_and_post()`:
   * Reads last 7 days of `outcomes`.
   * Calls claude-haiku-4-5 with a "summarize" prompt → markdown summary.
   * Posts via `slack_io.post_summary`.
3. Add `tests/test_learning.py` with at least 4 cases: (a) verified-true bumps weight by exactly +0.2, (b) verified-false multiplies weight by 0.9, (c) inconclusive leaves weight unchanged, (d) weight is clamped to \[0.1, 5.0\] at both ends.
4. Add a fifth test case: weekly summary mocks the haiku call + Slack post and asserts both are called exactly once with the expected 7-day input window.

**P — Plan:**

* `outcomes` table created lazily (additive migration in this PR; see `db_schema.sql` from AD-a).
* The summary cron runs every Monday 09:00 PT (configured via systemd timer in the PR's `deploy/` updates, but the timer config itself is out of scope — just expose `compose_and_post` so the timer can call it).
* Weight clamping is a hard floor/ceiling, never a soft penalty.

**E — Evidence:**

* `git diff` showing the four new files plus the additive schema migration.
* `pytest tests/test_learning.py -v` output: 5+ tests, all green.
* One real `compose_and_post()` dry-run with 7 synthetic outcomes producing a sample markdown digest pasted into the PR description.

**V — Verification (machine-checkable):**

1. Files `src/alfred_doctor/learning.py` and `src/alfred_doctor/weekly_summary.py` exist with the named classes and functions.
2. `pytest tests/test_learning.py -v` exits 0 with at least 5 test cases.
3. Tests assert weight delta arithmetic exactly: `+0.2` on true, `*0.9` on false, `0.0` on inconclusive.
4. Tests assert that weight is clamped to `[0.1, 5.0]` (parametrized at both extremes).
5. Tests assert that `compose_and_post` makes exactly one haiku gateway call and exactly one `slack_io.post_summary` call per invocation.
6. Plan doc `plans/v1-ga/AD-g.md` exists with an APE/V Acceptance section byte-identical to this Linear description's.
