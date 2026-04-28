# AD-d: Investigation loop (claude-opus open-ended prompt)

**Parent:** alfred-doctor epic — see `plans/v1-ga/AD.md`
**Linear:** [SAL-3284](https://linear.app/saluca/issue/SAL-3284)
**Wave:** wave-3

## Context

Step 3 of the architectural skeleton. When the surveillance loop (AD-b) flags `triggered=True`, the investigation loop fires a claude-opus-4-7 call with the full recent activity window plus the triggered findings, using an open-ended prompt: **"What surprises you? What patterns are emerging?"** The output is parsed into a list of `Surprise` records, each of which seeds a hypothesis tree (AD-f).

This is the deep + expensive loop. Cost is gated by the $20/day budget tracked in `token_usage`. If the budget is already spent, the loop posts a Slack alert and skips.

## Target paths

* `src/alfred_doctor/investigate.py`
* `src/alfred_doctor/prompts/investigate_prompt.txt`
* `tests/test_investigate.py`
* `plans/v1-ga/AD-d.md`

## Dependencies

Upstream: AD-a (reads `state.db`), AD-b (consumes `SurveyResult`), AD-c (Slack helper for budget alert).
Downstream: AD-f (consumes `list[Surprise]`), AD-h (renders findings on the dashboard).

## APE/V Acceptance

**A — Action:**

1. Add `src/alfred_doctor/prompts/investigate_prompt.txt` containing the open-ended investigation prompt template. Template variables: `{recent_activity}`, `{surveillance_findings}`. Must include the literal phrase `"What surprises you? What patterns are emerging?"`.
2. Add `src/alfred_doctor/investigate.py` with class `Investigator` exposing `Investigator.run(survey_result) -> InvestigationResult`. Method:
   * Checks `token_usage` rolling 24h sum vs $20 budget; if exceeded, calls `slack_io.post_for_approval` with a budget alert and returns `InvestigationResult(skipped=True, reason="budget_exceeded")`.
   * Otherwise, calls claude-opus-4-7 via the gateway with the prompt template hydrated from `state.db` recent activity + `survey_result.findings`.
   * Parses the response into `list[Surprise]` (each `Surprise` has `summary`, `evidence_refs`, `seed_question`).
3. `InvestigationResult` is a dataclass: `skipped: bool`, `reason: str`, `surprises: list[Surprise]`, `raw_response: str`.
4. Token usage logged to `token_usage` table.
5. Add `tests/test_investigate.py` with at least 3 cases covering: (a) prompt structure (template renders with both variables present and the surprises-question literal), (b) response parsing (mocked opus response with 2 surprises → 2 `Surprise` records), (c) recursive trigger logic / budget-exceeded path (mocked `token_usage` >$20 → `skipped=True`).

**P — Plan:**

* Reuse existing alfred-coo-svc gateway client for claude-opus-4-7.
* Recent activity window = last 4h of `ingest_events` (configurable via constant in module).
* Cost calc uses model-card pricing constants in `investigate.py` for claude-opus-4-7.

**E — Evidence:**

* `git diff` showing the four new files.
* `pytest tests/test_investigate.py -v` output: 3+ tests, all green.
* One real run with mocked surveillance trigger showing parsed `surprises` list.

**V — Verification (machine-checkable):**

1. File `src/alfred_doctor/investigate.py` exists with class `Investigator` and method `run` returning `InvestigationResult`.
2. The method calls claude-opus-4-7 via the gateway using the prompt template at `src/alfred_doctor/prompts/investigate_prompt.txt`.
3. The prompt template file contains the literal string `"What surprises you? What patterns are emerging?"`.
4. `pytest tests/test_investigate.py -v` exits 0 with at least 3 cases verifying prompt structure, response parsing, and budget-exceeded short-circuit.
5. Tests assert that `token_usage` row is appended on every non-skipped `Investigator.run` call.
6. Plan doc `plans/v1-ga/AD-d.md` exists with an APE/V Acceptance section byte-identical to this Linear description's.
