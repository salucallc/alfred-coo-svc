# AD-b: Surveillance loop with 12 known patterns (claude-haiku)

**Parent:** alfred-doctor epic — see `plans/v1-ga/AD.md`
**Linear:** [SAL-3282](https://linear.app/saluca/issue/SAL-3282)
**Wave:** wave-2

## Context

Step 2 of the architectural skeleton. Reads the last interval's delta from SQLite (written by AD-a), and uses claude-haiku-4-5 (fast + cheap, \~$0.25/M input) to score each of 12 known patterns from the 2026-04-28 mining sub findings. Severity 0..10. If any pattern scores >= 7, trigger the investigation loop (AD-d). If 4h has elapsed since the last deep-dig regardless of scores, trigger anyway (cap on stale).

## Target paths

* `src/alfred_doctor/surveillance.py`
* `src/alfred_doctor/patterns.yaml`
* `tests/test_surveillance.py`
* `plans/v1-ga/AD-b.md`

## Dependencies

Upstream: AD-a (must read from `state.db`).
Downstream: AD-d (consumes the trigger flag).

## APE/V Acceptance

**A — Action:**

1. Add `src/alfred_doctor/patterns.yaml` with the 12 patterns from the 2026-04-28 mining sub. Each entry: `id`, `description`, `severity_weight (default 1.0)`, `prompt_hint`. The 12 patterns: (1) silent-stall, (2) tool-output-override, (3) decomp-child-label-drift, (4) daemon-orphan-claim, (5) hawkman-syntactic-pass-only, (6) cost-spike-window, (7) mesh-restart-orphan, (8) registry-contradiction, (9) wave-skip-without-state-restore, (10) hint-mismatch-with-target, (11) APE/V-section-bytes-mismatch, (12) Linear-state-vs-PR-merge-mismatch.
2. Add `src/alfred_doctor/surveillance.py` with class `Surveyor` exposing `Surveyor.run(interval_id: str) -> SurveyResult`. Reads last interval's `ingest_events` rows, calls claude-haiku via the gateway with a prompt that scores all 12 patterns at once (one model call, JSON output: `{pattern_id: severity}`).
3. `SurveyResult` is a dataclass: `triggered: bool`, `findings: list[Finding]`, `reason: str` (either "severity>=7" or "4h-elapsed" or "no-trigger").
4. Token usage logged to `token_usage` table.
5. Add `tests/test_surveillance.py` with mocked claude-haiku response covering: (a) all-zero scores → no trigger, (b) one pattern at 8 → trigger with reason="severity>=7", (c) all-zero scores but >4h since last deep-dig → trigger with reason="4h-elapsed".

**P — Plan:**

* Reuse the existing alfred-coo-svc gateway client for claude-haiku-4-5 calls.
* Single batched prompt scoring all 12 patterns at once for cost efficiency.
* Last-deep-dig timestamp lives in a small `meta` row in `state.db` (key="last_investigate_at").

**E — Evidence:**

* `git diff` showing the four new files.
* `pytest tests/test_surveillance.py -v` output: 3+ tests, all green.
* One real run with live last interval rows showing a `SurveyResult` printed.

**V — Verification (machine-checkable):**

1. File `src/alfred_doctor/surveillance.py` exists with class `Surveyor` and method `run` returning a `SurveyResult` dataclass.
2. File `src/alfred_doctor/patterns.yaml` exists and contains exactly 12 entries with unique `id` values.
3. `pytest tests/test_surveillance.py -v` exits 0 with at least 3 test cases covering: all-zero (no trigger), one-high (severity trigger), 4h-elapsed (time trigger).
4. Tests assert that `token_usage` table receives a row per `Surveyor.run` call.
5. Plan doc `plans/v1-ga/AD-b.md` exists with an APE/V Acceptance section byte-identical to this Linear description's.
