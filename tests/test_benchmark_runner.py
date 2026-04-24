"""Plan M M-01/M-02 runner tests.

Covers:
  1. FixtureScript returns scripted response on matching tool call.
  2. FixtureScript raises UnscriptedToolCall on unknown tool.
  3. evaluate(transcript_assert) passes when must/not clauses all hold.
  4. evaluate(transcript_assert) fails and points at the offending clause when
     a forbidden tool was called.
  5. evaluate(terminal_form) passes when last turn is a pr_review tool call
     with the required ``state`` arg.
  6. M-MV-01's resolved_prompt_sha matches the current alfred-coo-a
     system prompt — catches persona drift.

Reference: Z:/_planning/v1-ga/M_model_benchmark_substrate.md §3.1 (schema) +
§3.3 (criterion kinds).
"""

from __future__ import annotations

import json

import pytest

from alfred_coo.benchmark.runner import (
    FixtureScript,
    UnscriptedToolCall,
    evaluate,
)
from alfred_coo.benchmark.schema import (
    Fixture,
    PassCriterion,
    ToolScriptEntry,
    compute_prompt_sha,
    load_fixture,
)


# ── Fixture-script interceptor ──────────────────────────────────────────────


def test_fixture_script_returns_scripted_response():
    fixture = load_fixture("M-MV-01")
    script = FixtureScript(fixture.tool_script)

    # http_get call against docker-compose.yml should return the scripted
    # 200 response with name set to docker-compose.yml.
    result = script.invoke(
        "http_get",
        {"url": "https://api.github.com/repos/salucallc/alfred-coo-svc/contents/deploy/appliance/docker-compose.yml?ref=main"},
    )

    assert result["status"] == 200
    assert result["body"]["name"] == "docker-compose.yml"
    assert result["body"]["path"] == "deploy/appliance/docker-compose.yml"
    # call_log captured the invocation for later introspection
    assert script.call_log and script.call_log[0]["tool"] == "http_get"


def test_fixture_script_unscripted_tool_raises():
    fixture = load_fixture("M-MV-01")
    script = FixtureScript(fixture.tool_script)

    # slack_post is not in M-MV-01's tool_script — the interceptor must
    # raise UnscriptedToolCall. Plan M §3.2: "an uncovered tool call →
    # fixture fails with reason unexpected_tool_call".
    with pytest.raises(UnscriptedToolCall) as excinfo:
        script.invoke("slack_post", {"text": "hello"})
    assert excinfo.value.tool == "slack_post"
    assert excinfo.value.arguments == {"text": "hello"}


# ── Evaluator: transcript_assert ────────────────────────────────────────────


def _make_transcript_assert_fixture() -> Fixture:
    """Build an in-memory Fixture wrapping M-MV-01's pass criterion, with
    the JSON loader path so we exercise the real schema parse.

    We copy the real fixture and swap only the parts we need for the
    targeted test cases.
    """
    return load_fixture("M-MV-01")


def test_evaluate_transcript_assert_pass():
    fixture = _make_transcript_assert_fixture()
    # Synthetic transcript: model called propose_pr, never called
    # linear_create_issue, and its text did not contain ".yaml".
    transcript = {
        "content": "Opened PR at https://github.com/salucallc/alfred-coo-svc/pull/999 with the docker-compose.yml env-var block update. Verified against ## Target.",
        "tool_calls": [
            {"iteration": 0, "name": "http_get", "arguments": json.dumps({"url": "https://raw.githubusercontent.com/.../OPS-01.md"}), "result": "{}"},
            {"iteration": 1, "name": "http_get", "arguments": json.dumps({"url": "https://api.github.com/repos/salucallc/alfred-coo-svc/contents/deploy/appliance/docker-compose.yml?ref=main"}), "result": "{}"},
            {"iteration": 2, "name": "propose_pr", "arguments": json.dumps({"branch": "feature/sal-2634-compose-env"}), "result": "{}"},
        ],
    }

    score = evaluate(fixture, transcript)
    assert score.passed, f"expected pass, got reasons={score.reasons!r}"
    assert score.reasons == []
    assert "propose_pr" in score.details["tool_names"]


def test_evaluate_transcript_assert_fail_on_forbidden_tool():
    fixture = _make_transcript_assert_fixture()
    # The model escalated to linear_create_issue — M-MV-01's asserts
    # forbid that. Expected: passed=False with a reason pointing at the
    # not: linear_create_issue clause AND the not: \\.yaml clause (model
    # prose says ".yaml" too).
    transcript = {
        "content": "The target file is docker-compose.yaml — opening a grounding gap.",
        "tool_calls": [
            {"iteration": 0, "name": "http_get", "arguments": json.dumps({"url": "..."}), "result": "{}"},
            {"iteration": 1, "name": "linear_create_issue", "arguments": json.dumps({"title": "grounding gap"}), "result": "{}"},
        ],
    }

    score = evaluate(fixture, transcript)
    assert not score.passed
    # At least one reason must cite the forbidden tool.
    assert any("linear_create_issue" in r for r in score.reasons), score.reasons
    # And a reason must cite the content_regex .yaml clause.
    assert any("yaml" in r for r in score.reasons), score.reasons


# ── Evaluator: terminal_form ────────────────────────────────────────────────


def test_evaluate_terminal_form_pr_review():
    fixture = load_fixture("M-MV-02")
    # Transcript: pr_review tool call with state set as the last turn.
    transcript = {
        "content": "",  # pr_review tool call closed the loop
        "tool_calls": [
            {"iteration": 0, "name": "pr_files_get", "arguments": "{}", "result": "{}"},
            {"iteration": 1, "name": "pr_review", "arguments": json.dumps({"state": "APPROVE", "body": "gates pass"}), "result": "{}"},
        ],
    }

    score = evaluate(fixture, transcript)
    assert score.passed, f"expected pass, got reasons={score.reasons!r}"
    assert score.details.get("matched_form") is not None


# ── Fixture-SHA integrity ───────────────────────────────────────────────────


def test_resolved_prompt_sha_matches_persona():
    """Catches persona drift: if alfred-coo-a's system prompt changes but
    the fixture isn't re-locked, this test fails (Plan M §8 R-3/R-4)."""
    fixture = load_fixture("M-MV-01")
    current = compute_prompt_sha("alfred-coo-a")
    assert fixture.resolved_prompt_sha == current, (
        "M-MV-01 resolved_prompt_sha is stale; run gen_fixtures to re-lock "
        "or confirm the persona edit was intentional."
    )
