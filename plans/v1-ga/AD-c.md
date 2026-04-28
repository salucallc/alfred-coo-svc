# AD-c: Slack integration for Tier-2 alerts

**Parent:** alfred-doctor epic — see `plans/v1-ga/AD.md`
**Linear:** [SAL-3283](https://linear.app/saluca/issue/SAL-3283)
**Wave:** wave-2

## Context

Step 7 of the architectural skeleton (broken out as a standalone helper for testability). Provides a thin Slack helper that posts Tier-2 reasoning chains into `#batcave` (channel `C0ASAKFTR1C`) and observes thread replies for `approve` / `deny`. Reused by the action layer (AD-e) and the weekly summary (AD-g). Does NOT make decisions; it only sends + reads.

## Target paths

* `src/alfred_doctor/slack_io.py`
* `tests/test_slack_io.py`
* `plans/v1-ga/AD-c.md`

## Dependencies

Upstream: AD-a (only for shared env loading; logically independent).
Downstream: AD-e (calls `post_for_approval`), AD-g (calls `post_summary`).

## APE/V Acceptance

**A — Action:**

1. Add `src/alfred_doctor/slack_io.py` with three functions:
   * `post_for_approval(reasoning: str, action_summary: str) -> str`: posts to `#batcave` with reasoning + action summary, returns the message `ts` so the caller can poll the thread.
   * `read_approval(thread_ts: str, timeout_seconds: int = 1800) -> Literal["approve", "deny", "timeout"]`: polls thread replies; returns the first reply containing the literal token `approve` or `deny` from Cristian's user ID `U0AH88KHZ4H`. Times out after `timeout_seconds`.
   * `post_summary(summary_md: str) -> str`: posts a weekly self-summary to `#batcave`.
2. Use `SLACK_BOT_TOKEN_ALFRED` env var (per the batcave-cadence memory).
3. All Slack errors raise `SlackError` (custom exception); callers decide whether to retry or escalate.
4. Add `tests/test_slack_io.py` with mocked Slack HTTP responses for all three functions, including: approve-path, deny-path, timeout-path, network-error-path.

**P — Plan:**

* Use stdlib `urllib.request` plus `slack_sdk` if it is already a dep; check `pyproject.toml` first.
* Cristian's user ID is hardcoded per the `reference_cristian_slack_user_id` memory.
* Channel ID is hardcoded as `C0ASAKFTR1C`.

**E — Evidence:**

* `git diff` showing the three new files.
* `pytest tests/test_slack_io.py -v` output: 4+ tests, all green.
* One real test post in `#batcave` with a "\[test\] alfred-doctor slack-io smoke" prefix that Cristian can `approve` to demonstrate the round-trip (captured in PR description).

**V — Verification (machine-checkable):**

1. File `src/alfred_doctor/slack_io.py` exists exposing exactly three public functions: `post_for_approval`, `read_approval`, `post_summary`.
2. `read_approval` returns one of the three string literals: `"approve"`, `"deny"`, `"timeout"`.
3. `pytest tests/test_slack_io.py -v` exits 0 with at least 4 test cases covering approve / deny / timeout / network-error.
4. Tests assert that approval is only accepted from user `U0AH88KHZ4H` (any other user's reply is ignored).
5. Plan doc `plans/v1-ga/AD-c.md` exists with an APE/V Acceptance section byte-identical to this Linear description's.
