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


# ── B.3.2: mesh_task_create + propose_pr ────────────────────────────────────

from alfred_coo.tools import mesh_task_create, propose_pr, _safe_workspace_path


@pytest.mark.asyncio
async def test_mesh_task_create_missing_key_returns_error(monkeypatch):
    monkeypatch.delenv("SOUL_API_KEY", raising=False)
    result = await mesh_task_create(title="test")
    assert "error" in result
    assert "SOUL_API_KEY" in result["error"]


@pytest.mark.asyncio
async def test_mesh_task_create_new_tool_registered():
    assert "mesh_task_create" in BUILTIN_TOOLS
    assert "propose_pr" in BUILTIN_TOOLS
    schema = openai_tool_schema(BUILTIN_TOOLS["propose_pr"])
    required = schema["function"]["parameters"]["required"]
    for key in ("owner", "repo", "branch", "title", "body", "files"):
        assert key in required


@pytest.mark.asyncio
async def test_propose_pr_rejects_bad_owner(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    result = await propose_pr(
        owner="evil-org",
        repo="hack",
        branch="x",
        title="x",
        body="x",
        files={"a.md": "hi"},
    )
    assert "error" in result
    assert "allowlist" in result["error"]


@pytest.mark.asyncio
async def test_propose_pr_rejects_missing_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = await propose_pr(
        owner="salucallc",
        repo="x",
        branch="b",
        title="t",
        body="b",
        files={"a.md": "hi"},
    )
    assert "error" in result
    assert "GITHUB_TOKEN" in result["error"]


@pytest.mark.asyncio
async def test_propose_pr_rejects_empty_files(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    result = await propose_pr(
        owner="salucallc",
        repo="x",
        branch="b",
        title="t",
        body="b",
        files={},
    )
    assert "error" in result
    assert "files" in result["error"]


@pytest.mark.asyncio
async def test_propose_pr_rejects_invalid_branch(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    result = await propose_pr(
        owner="salucallc",
        repo="x",
        branch="bad branch name with space",
        title="t",
        body="b",
        files={"a.md": "hi"},
    )
    assert "error" in result
    assert "branch" in result["error"]


def test_safe_workspace_path_rejects_escape(tmp_path):
    assert _safe_workspace_path(tmp_path, "../escape.md") is None
    assert _safe_workspace_path(tmp_path, "/absolute.md") is None
    assert _safe_workspace_path(tmp_path, "C:/drive.md") is None
    assert _safe_workspace_path(tmp_path, "") is None
    assert _safe_workspace_path(tmp_path, None) is None  # type: ignore[arg-type]


def test_safe_workspace_path_accepts_relative(tmp_path):
    assert _safe_workspace_path(tmp_path, "a.md") is not None
    assert _safe_workspace_path(tmp_path, "sub/b.py") is not None


# ── B.3.3: task-scoped workspaces via ContextVar ───────────────────────────

from alfred_coo.tools import (
    get_current_task_id,
    reset_current_task_id,
    set_current_task_id,
)


def test_current_task_id_default_is_none():
    assert get_current_task_id() is None


def test_current_task_id_set_and_reset_roundtrip():
    token = set_current_task_id("task-123")
    try:
        assert get_current_task_id() == "task-123"
    finally:
        reset_current_task_id(token)
    assert get_current_task_id() is None


@pytest.mark.asyncio
async def test_current_task_id_isolated_per_task():
    """Concurrent 'tasks' each see their own task_id via asyncio context isolation."""
    async def task_body(tid: str) -> str:
        token = set_current_task_id(tid)
        try:
            await asyncio.sleep(0.01)  # yield so the scheduler interleaves both
            return get_current_task_id()
        finally:
            reset_current_task_id(token)

    a, b = await asyncio.gather(task_body("A"), task_body("B"))
    assert a == "A"
    assert b == "B"
    # And after both finished, the outer context is still clean.
    assert get_current_task_id() is None
