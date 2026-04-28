"""SAL-3140: enforce propose_pr / update_pr / linear_create_issue gate on
builder dispatches.

Tests the pure helper functions
``alfred_coo.main._extract_tool_call_names`` and
``alfred_coo.main._builder_envelope_only_completion``. These are the gate's
decision surface — the poll-loop calls
``_builder_envelope_only_completion`` and trusts its boolean to mark a
task failed and re-queue it. We assert the contract:

1. Builder dispatch ([persona:alfred-coo-a] + [tag:code]) with NO required
   tool in the result must trigger the gate.
2. Builder dispatch with at least one required tool (propose_pr,
   update_pr, linear_create_issue) must NOT trigger the gate.
3. Non-builder personas (e.g. hawkman-qa-a) must NOT trigger the gate
   regardless of tool calls.
4. Tool-call entries that come through in the legacy
   ``{"function": {"name": ...}}`` shape must still be detected by name
   so the gate doesn't false-fire on a valid OpenAI passthrough envelope.
"""

from __future__ import annotations

from alfred_coo.main import (
    _BUILDER_REQUIRED_TOOLS,
    _builder_envelope_only_completion,
    _extract_tool_call_names,
)


# ── _extract_tool_call_names ────────────────────────────────────────────────


def test_extract_tool_call_names_flat_shape():
    """`tool_calls` from `dispatch.call_with_tools.tool_call_log` are flat
    dicts with `name` at the top level."""
    result = {
        "tool_calls": [
            {"iteration": 0, "name": "http_get", "arguments": "{}", "result": "{}"},
            {"iteration": 1, "name": "propose_pr", "arguments": "{}", "result": "{}"},
        ]
    }
    assert _extract_tool_call_names(result) == ["http_get", "propose_pr"]


def test_extract_tool_call_names_openai_function_shape():
    """OpenAI passthrough has `function.name` instead of top-level `name`."""
    result = {
        "tool_calls": [
            {"function": {"name": "http_get", "arguments": "{}"}},
            {"function": {"name": "propose_pr", "arguments": "{}"}},
        ]
    }
    assert _extract_tool_call_names(result) == ["http_get", "propose_pr"]


def test_extract_tool_call_names_missing_or_malformed():
    assert _extract_tool_call_names({}) == []
    assert _extract_tool_call_names({"tool_calls": None}) == []
    assert _extract_tool_call_names({"tool_calls": "not-a-list"}) == []
    # Skip non-dict entries silently.
    assert _extract_tool_call_names({"tool_calls": ["string", 42, None, {"name": "ok"}]}) == ["ok"]
    # Skip entries with neither `name` nor `function.name`.
    assert _extract_tool_call_names({"tool_calls": [{"arguments": "{}"}]}) == []


# ── _builder_envelope_only_completion ───────────────────────────────────────


def test_gate_rejects_builder_with_no_required_tool():
    """Bug case: kimi/qwen builder emits artifacts envelope, only http_get
    in tool_calls, no propose_pr. Gate must fire."""
    result = {
        "tool_calls": [
            {"iteration": 0, "name": "http_get", "arguments": "{}", "result": "{}"},
            {"iteration": 1, "name": "http_get", "arguments": "{}", "result": "{}"},
        ],
        "model_used": "kimi-k2-thinking:cloud",
    }
    failed, observed = _builder_envelope_only_completion(
        persona_name="alfred-coo-a",
        task_title="[persona:alfred-coo-a] [tag:code] SAL-3072: do the thing",
        result=result,
    )
    assert failed is True
    assert observed == ["http_get", "http_get"]


def test_gate_passes_builder_with_propose_pr():
    """Happy path: builder fired propose_pr. Gate must NOT fire."""
    result = {
        "tool_calls": [
            {"iteration": 0, "name": "http_get", "arguments": "{}", "result": "{}"},
            {"iteration": 5, "name": "propose_pr", "arguments": "{}", "result": "{}"},
        ]
    }
    failed, observed = _builder_envelope_only_completion(
        persona_name="alfred-coo-a",
        task_title="[persona:alfred-coo-a] [tag:code] SAL-3072: do the thing",
        result=result,
    )
    assert failed is False
    assert "propose_pr" in observed


def test_gate_passes_builder_with_update_pr():
    result = {
        "tool_calls": [
            {"iteration": 1, "name": "update_pr", "arguments": "{}", "result": "{}"},
        ]
    }
    failed, _ = _builder_envelope_only_completion(
        persona_name="alfred-coo-a",
        task_title="[persona:alfred-coo-a] [tag:code] SAL-3072: fix-round 2",
        result=result,
    )
    assert failed is False


def test_gate_passes_builder_with_linear_create_issue_escalation():
    """Escalate path: builder identifies grounding gap and creates a Linear
    issue instead of writing a PR. Gate must NOT fire."""
    result = {
        "tool_calls": [
            {"iteration": 0, "name": "linear_create_issue", "arguments": "{}", "result": "{}"},
        ]
    }
    failed, _ = _builder_envelope_only_completion(
        persona_name="alfred-coo-a",
        task_title="[persona:alfred-coo-a] [tag:code] SAL-3069: missing target",
        result=result,
    )
    assert failed is False


def test_gate_does_not_apply_to_non_builder_persona():
    """hawkman-qa-a (and every other non-builder persona) is exempt.
    QA doesn't open PRs — it gates them. We must not nuke a valid QA pass
    just because it didn't call propose_pr."""
    result = {
        "tool_calls": [
            {"iteration": 0, "name": "http_get", "arguments": "{}", "result": "{}"},
        ]
    }
    failed, _ = _builder_envelope_only_completion(
        persona_name="hawkman-qa-a",
        task_title="[persona:hawkman-qa-a] [tag:code] SAL-3072: review PR #200",
        result=result,
    )
    assert failed is False


def test_gate_does_not_apply_to_builder_without_tag_code():
    """Builder tasks lacking `[tag:code]` (e.g. strategy-tagged or untagged)
    are not subject to the propose_pr contract."""
    result = {"tool_calls": []}
    failed, _ = _builder_envelope_only_completion(
        persona_name="alfred-coo-a",
        task_title="[persona:alfred-coo-a] [tag:strategy] SAL-X: think out loud",
        result=result,
    )
    assert failed is False


def test_gate_handles_empty_and_missing_tool_calls():
    """The bug's exact signature: zero tool_calls, just an envelope summary."""
    failed, observed = _builder_envelope_only_completion(
        persona_name="alfred-coo-a",
        task_title="[persona:alfred-coo-a] [tag:code] SAL-3130: foo",
        result={"tool_calls": []},
    )
    assert failed is True
    assert observed == []

    failed, observed = _builder_envelope_only_completion(
        persona_name="alfred-coo-a",
        task_title="[persona:alfred-coo-a] [tag:code] SAL-3130: foo",
        result={},
    )
    assert failed is True
    assert observed == []


def test_required_tools_constant_is_canonical():
    """Lock the required-tool set so future edits are explicit. The persona
    contract at persona.py:60-66 names exactly these three tools."""
    assert _BUILDER_REQUIRED_TOOLS == frozenset(
        {"propose_pr", "update_pr", "linear_create_issue"}
    )


def test_gate_detects_required_tool_in_openai_function_shape():
    """If a model emits the OpenAI tool-call shape (function.name), the gate
    must still recognise propose_pr by name and not false-fire."""
    result = {
        "tool_calls": [
            {"function": {"name": "propose_pr", "arguments": "{}"}},
        ]
    }
    failed, observed = _builder_envelope_only_completion(
        persona_name="alfred-coo-a",
        task_title="[persona:alfred-coo-a] [tag:code] SAL-3072: do",
        result=result,
    )
    assert failed is False
    assert "propose_pr" in observed
