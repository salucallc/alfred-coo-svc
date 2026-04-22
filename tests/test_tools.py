"""Tool registry + handler tests.

HTTP-hitting handlers (linear_create_issue, slack_post) are exercised via their
error paths — missing credentials — which is all we can deterministically test
without network. End-to-end tool invocation is covered in the B.3 smoke-test
task after deployment.
"""

import asyncio
import json
import os

import pytest

from alfred_coo.tools import (
    BUILTIN_TOOLS,
    ToolSpec,
    execute_tool,
    linear_create_issue,
    openai_tool_schema,
    resolve_tools,
    slack_post,
)


def test_builtin_tools_registered():
    assert "linear_create_issue" in BUILTIN_TOOLS
    assert "slack_post" in BUILTIN_TOOLS


def test_openai_schema_shape():
    schema = openai_tool_schema(BUILTIN_TOOLS["linear_create_issue"])
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "linear_create_issue"
    assert "title" in schema["function"]["parameters"]["properties"]
    assert "title" in schema["function"]["parameters"]["required"]


def test_resolve_tools_skips_unknown():
    tools = resolve_tools(["linear_create_issue", "does_not_exist", "slack_post"])
    names = [t.name for t in tools]
    assert names == ["linear_create_issue", "slack_post"]


def test_resolve_tools_handles_empty_and_none():
    assert resolve_tools([]) == []
    assert resolve_tools(None) == []  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_linear_missing_key_returns_error(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("ALFRED_OPS_LINEAR_API_KEY", raising=False)
    result = await linear_create_issue(title="test")
    assert "error" in result
    assert "LINEAR_API_KEY" in result["error"]


@pytest.mark.asyncio
async def test_slack_missing_token_returns_error(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN_ALFRED", raising=False)
    result = await slack_post(message="hi")
    assert "error" in result
    assert "SLACK_BOT_TOKEN" in result["error"]


@pytest.mark.asyncio
async def test_execute_tool_catches_bad_json():
    spec = BUILTIN_TOOLS["slack_post"]
    out = await execute_tool(spec, "{not valid}")
    body = json.loads(out)
    assert "error" in body
    assert "bad arguments JSON" in body["error"]


@pytest.mark.asyncio
async def test_execute_tool_catches_bad_args_type():
    spec = BUILTIN_TOOLS["slack_post"]
    out = await execute_tool(spec, '"just a string"')
    body = json.loads(out)
    assert "error" in body


@pytest.mark.asyncio
async def test_execute_tool_catches_handler_exception(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("synthetic failure")

    bad_spec = ToolSpec(
        name="boom",
        description="always fails",
        parameters={"type": "object", "properties": {}},
        handler=boom,
    )
    out = await execute_tool(bad_spec, "{}")
    body = json.loads(out)
    assert "error" in body
    assert "synthetic failure" in body["error"]


@pytest.mark.asyncio
async def test_execute_tool_catches_argument_mismatch():
    # linear_create_issue requires 'title'; invoking with no args should fail on the handler signature.
    spec = BUILTIN_TOOLS["linear_create_issue"]
    out = await execute_tool(spec, "{}")
    body = json.loads(out)
    assert "error" in body
    # Either TypeError from missing title OR the missing-key error after falling through
    assert "title" in body["error"].lower() or "LINEAR_API_KEY" in body["error"]
