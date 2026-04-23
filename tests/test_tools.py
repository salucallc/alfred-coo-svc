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


# ── B.3.4: http_get allowlist ───────────────────────────────────────────────

from alfred_coo.tools import _is_allowed_http_url, http_get


def test_http_allowlist_accepts_saluca_github():
    for url in (
        "https://github.com/salucallc/soul-svc/blob/main/README.md",
        "https://github.com/saluca-labs/tiresias-core",
        "https://github.com/cristianxruvalcaba-coder/alfred-portal",
        "https://raw.githubusercontent.com/salucallc/mcp-gateway/main/README.md",
        "https://api.github.com/repos/salucallc/soul-svc/contents/README.md",
    ):
        ok, reason = _is_allowed_http_url(url)
        assert ok, f"expected allowed: {url} ({reason})"


def test_http_allowlist_accepts_saluca_domains():
    for url in (
        "https://saluca.com/",
        "https://www.saluca.com/pricing",
        "https://asphodel.ai/docs",
        "https://platform.tiresias.network/api",
    ):
        ok, reason = _is_allowed_http_url(url)
        assert ok, f"expected allowed: {url} ({reason})"


def test_http_allowlist_accepts_docs_and_arxiv():
    for url in (
        "https://arxiv.org/abs/2024.12345",
        "https://docs.anthropic.com/en/api",
        "https://docs.python.org/3/library/contextvars.html",
    ):
        ok, reason = _is_allowed_http_url(url)
        assert ok, f"expected allowed: {url} ({reason})"


def test_http_allowlist_rejects_arbitrary_github_user():
    ok, reason = _is_allowed_http_url("https://github.com/some-random-user/repo")
    assert not ok
    assert "not in Saluca allowlist" in reason


def test_http_allowlist_rejects_arbitrary_host():
    ok, reason = _is_allowed_http_url("https://malicious.example.com/payload")
    assert not ok
    assert "not in allowlist" in reason


def test_http_allowlist_rejects_non_http_schemes():
    for url in ("file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)", ""):
        ok, _ = _is_allowed_http_url(url)
        assert not ok


def test_http_allowlist_normalises_case_and_strips_userinfo():
    ok, _ = _is_allowed_http_url("https://user@GitHub.com/salucallc/soul-svc")
    assert ok


def test_http_allowlist_rejects_wrong_github_subdomain():
    # gist.github.com isn't in the allowlist even though it looks adjacent.
    ok, reason = _is_allowed_http_url("https://gist.github.com/salucallc/123")
    assert not ok


@pytest.mark.asyncio
async def test_http_get_rejects_before_network_call():
    # No network at all if the URL isn't allowed — immediate error return.
    result = await http_get("https://example.com/")
    assert "error" in result
    assert "allowlist" in result["error"]


# ── B.3.5: pr_review ────────────────────────────────────────────────────────

from alfred_coo.tools import pr_review


def test_pr_review_registered():
    assert "pr_review" in BUILTIN_TOOLS


def test_pr_review_schema():
    schema = openai_tool_schema(BUILTIN_TOOLS["pr_review"])
    assert schema["function"]["name"] == "pr_review"
    required = schema["function"]["parameters"]["required"]
    for key in ("owner", "repo", "pr_number", "event", "body"):
        assert key in required, f"pr_review schema missing required field: {key}"


@pytest.mark.asyncio
async def test_pr_review_rejects_bad_owner(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    result = await pr_review(
        owner="evil-org",
        repo="hack",
        pr_number=1,
        event="APPROVE",
        body="looks good",
    )
    assert "error" in result
    assert "allowlist" in result["error"]


@pytest.mark.asyncio
async def test_pr_review_rejects_invalid_event(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    result = await pr_review(
        owner="salucallc",
        repo="alfred-coo-svc",
        pr_number=1,
        event="MERGE_IT",
        body="ship it",
    )
    assert "error" in result
    assert "event" in result["error"]


@pytest.mark.asyncio
async def test_pr_review_missing_token_returns_error(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = await pr_review(
        owner="salucallc",
        repo="alfred-coo-svc",
        pr_number=1,
        event="COMMENT",
        body="hi",
    )
    assert "error" in result
    assert "GITHUB_TOKEN" in result["error"]


# ── B.3.6: pr_files_get ─────────────────────────────────────────────────────

from alfred_coo.tools import pr_files_get


def test_pr_files_get_registered():
    assert "pr_files_get" in BUILTIN_TOOLS


def test_pr_files_get_schema():
    schema = openai_tool_schema(BUILTIN_TOOLS["pr_files_get"])
    assert schema["function"]["name"] == "pr_files_get"
    params = schema["function"]["parameters"]
    required = params["required"]
    for key in ("owner", "repo", "pr_number"):
        assert key in required, f"pr_files_get schema missing required field: {key}"
    assert params["properties"]["pr_number"]["type"] == "integer"


@pytest.mark.asyncio
async def test_pr_files_get_rejects_bad_owner(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    result = await pr_files_get(
        owner="evil-org",
        repo="hack",
        pr_number=1,
    )
    assert "error" in result
    assert "allowlist" in result["error"]


@pytest.mark.asyncio
async def test_pr_files_get_rejects_missing_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = await pr_files_get(
        owner="salucallc",
        repo="alfred-coo-svc",
        pr_number=1,
    )
    assert "error" in result
    assert "GITHUB_TOKEN" in result["error"]


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


# ── AB-03: slack_ack_poll + linear_* helpers ────────────────────────────────

from alfred_coo.tools import (  # noqa: E402
    linear_get_issue_relations,
    linear_list_project_issues,
    linear_update_issue_state,
    slack_ack_poll,
)
from alfred_coo.tools import _LINEAR_TEAM_STATES_CACHE  # noqa: E402


def test_ab03_tools_registered():
    for name in (
        "slack_ack_poll",
        "linear_update_issue_state",
        "linear_list_project_issues",
        "linear_get_issue_relations",
    ):
        assert name in BUILTIN_TOOLS, f"{name} missing from BUILTIN_TOOLS"


def test_slack_ack_poll_schema():
    schema = openai_tool_schema(BUILTIN_TOOLS["slack_ack_poll"])
    assert schema["function"]["name"] == "slack_ack_poll"
    required = schema["function"]["parameters"]["required"]
    for key in ("channel", "after_ts", "author_user_id", "keywords"):
        assert key in required


class _FakeHTTPResponse:
    """Minimal stand-in for urllib.request.urlopen return value."""

    def __init__(self, payload: dict, status: int = 200):
        self._payload = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_urlopen_queue(monkeypatch, responses):
    """Install an urlopen stub that hands back queued responses in order.

    Each element of `responses` is either a dict (treated as a 200 JSON body)
    or a pre-built `_FakeHTTPResponse`. Returns the list of captured Request
    objects for assertions.
    """
    import urllib.request as _ur

    captured = []
    queue = list(responses)

    def fake_urlopen(req, timeout=None):
        captured.append(req)
        if not queue:
            raise AssertionError("urlopen called more times than responses queued")
        nxt = queue.pop(0)
        if isinstance(nxt, _FakeHTTPResponse):
            return nxt
        return _FakeHTTPResponse(nxt)

    monkeypatch.setattr(_ur, "urlopen", fake_urlopen)
    return captured


@pytest.mark.asyncio
async def test_slack_ack_poll_missing_token(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN_ALFRED", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    result = await slack_ack_poll(
        channel="C123",
        after_ts="0.0",
        author_user_id="U1",
        keywords=["ack"],
    )
    assert "error" in result
    assert "SLACK_BOT_TOKEN_ALFRED" in result["error"]


@pytest.mark.asyncio
async def test_slack_ack_poll_matches_keyword(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN_ALFRED", "xoxb-fake")
    _install_urlopen_queue(monkeypatch, [{
        "ok": True,
        "has_more": False,
        "messages": [
            # Slack returns newest-first; the older-by-time message will match.
            {"ts": "200.0", "user": "U1", "text": "other chatter"},
            {"ts": "150.0", "user": "U1", "text": "ACK SS-08 proceed"},
            {"ts": "100.0", "user": "U2", "text": "unrelated"},
        ],
    }])
    result = await slack_ack_poll(
        channel="C0ASAKFTR1C",
        after_ts="90.0",
        author_user_id="U1",
        keywords=[r"(?i)(ack|approve)\s*ss[-_ ]?08"],
    )
    assert result["matched"] is True
    assert result["message_ts"] == "150.0"
    assert "SS-08" in result["text"] or "ss-08" in result["text"].lower()
    assert result["matched_keyword"] == r"(?i)(ack|approve)\s*ss[-_ ]?08"


@pytest.mark.asyncio
async def test_slack_ack_poll_returns_none_when_no_match(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN_ALFRED", "xoxb-fake")
    _install_urlopen_queue(monkeypatch, [{
        "ok": True,
        "has_more": False,
        "messages": [
            {"ts": "110.0", "user": "U1", "text": "still reviewing"},
            {"ts": "105.0", "user": "U2", "text": "ACK SS-08"},  # wrong author
        ],
    }])
    result = await slack_ack_poll(
        channel="C0ASAKFTR1C",
        after_ts="100.0",
        author_user_id="U1",
        keywords=[r"ack"],
    )
    assert result == {"matched": False}


@pytest.mark.asyncio
async def test_slack_ack_poll_paginates_on_cursor(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN_ALFRED", "xoxb-fake")
    captured = _install_urlopen_queue(monkeypatch, [
        {
            "ok": True,
            "has_more": True,
            "response_metadata": {"next_cursor": "dXNlcjpVMDYxTkZUVDI="},
            "messages": [
                {"ts": "120.0", "user": "U1", "text": "working on it"},
            ],
        },
        {
            "ok": True,
            "has_more": False,
            "messages": [
                {"ts": "180.0", "user": "U1", "text": "approve ss-08 go"},
            ],
        },
    ])
    result = await slack_ack_poll(
        channel="C0ASAKFTR1C",
        after_ts="100.0",
        author_user_id="U1",
        keywords=[r"approve"],
    )
    assert result["matched"] is True
    assert result["message_ts"] == "180.0"
    assert len(captured) == 2
    assert "cursor=" in captured[1].full_url
    assert "dXNlcjpVMDYxTkZUVDI" in captured[1].full_url


@pytest.mark.asyncio
async def test_linear_update_issue_state_missing_key(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("ALFRED_OPS_LINEAR_API_KEY", raising=False)
    result = await linear_update_issue_state("SAL-2680", "In Progress")
    assert "error" in result
    assert "LINEAR_API_KEY" in result["error"]


@pytest.mark.asyncio
async def test_linear_update_issue_state_resolves_and_mutates(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_fake")
    _LINEAR_TEAM_STATES_CACHE.clear()
    state_in_progress = "state-uuid-in-progress"
    team_uuid = "team-uuid-sal"
    _install_urlopen_queue(monkeypatch, [
        # 1. issue lookup
        {"data": {"issue": {
            "id": "issue-uuid-2680",
            "identifier": "SAL-2680",
            "team": {"id": team_uuid},
            "state": {"name": "Todo"},
        }}},
        # 2. team states lookup (cache miss)
        {"data": {"team": {
            "id": team_uuid,
            "name": "SAL",
            "states": {"nodes": [
                {"id": "state-uuid-backlog", "name": "Backlog", "type": "backlog"},
                {"id": "state-uuid-todo", "name": "Todo", "type": "unstarted"},
                {"id": state_in_progress, "name": "In Progress", "type": "started"},
                {"id": "state-uuid-done", "name": "Done", "type": "completed"},
            ]},
        }}},
        # 3. issueUpdate mutation
        {"data": {"issueUpdate": {
            "success": True,
            "issue": {
                "identifier": "SAL-2680",
                "state": {"name": "In Progress"},
            },
        }}},
    ])
    result = await linear_update_issue_state("SAL-2680", "In Progress")
    assert result == {"ok": True, "identifier": "SAL-2680", "state": "In Progress"}
    # Second call with same team hits cache — queue only holds 2 responses now
    # (no team lookup re-run).
    _install_urlopen_queue(monkeypatch, [
        {"data": {"issue": {
            "id": "issue-uuid-2681",
            "identifier": "SAL-2681",
            "team": {"id": team_uuid},
            "state": {"name": "Backlog"},
        }}},
        {"data": {"issueUpdate": {
            "success": True,
            "issue": {"identifier": "SAL-2681", "state": {"name": "In Progress"}},
        }}},
    ])
    result2 = await linear_update_issue_state("SAL-2681", "In Progress")
    assert result2["ok"] is True
    assert team_uuid in _LINEAR_TEAM_STATES_CACHE


@pytest.mark.asyncio
async def test_linear_update_issue_state_unknown_state(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_fake")
    _LINEAR_TEAM_STATES_CACHE.clear()
    _install_urlopen_queue(monkeypatch, [
        {"data": {"issue": {
            "id": "issue-uuid",
            "identifier": "SAL-1",
            "team": {"id": "team-x"},
            "state": {"name": "Todo"},
        }}},
        {"data": {"team": {
            "id": "team-x",
            "name": "SAL",
            "states": {"nodes": [
                {"id": "s1", "name": "Backlog", "type": "backlog"},
                {"id": "s2", "name": "Done", "type": "completed"},
            ]},
        }}},
    ])
    result = await linear_update_issue_state("SAL-1", "Flying")
    assert "error" in result
    assert "not found" in result["error"]
    assert "available_states" in result
    assert "backlog" in result["available_states"]


@pytest.mark.asyncio
async def test_linear_list_project_issues_paginates(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_fake")

    def make_page(ids, has_next, cursor):
        return {"data": {"project": {
            "id": "proj-uuid",
            "name": "Mission Control v1 GA",
            "issues": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                "nodes": [
                    {
                        "id": f"uuid-{i}",
                        "identifier": f"SAL-{i}",
                        "title": f"ticket {i}",
                        "estimate": 3,
                        "labels": {"nodes": [{"name": "wave-0"}]},
                        "state": {"name": "Backlog"},
                        "relations": {"nodes": []},
                    }
                    for i in ids
                ],
            },
        }}}

    _install_urlopen_queue(monkeypatch, [
        make_page([1, 2, 3], True, "cursor-page-1"),
        make_page([4, 5], False, None),
    ])
    result = await linear_list_project_issues("proj-uuid", limit=10)
    assert result["total"] == 5
    assert result["truncated"] is False
    identifiers = [i["identifier"] for i in result["issues"]]
    assert identifiers == ["SAL-1", "SAL-2", "SAL-3", "SAL-4", "SAL-5"]
    # label + state shape preserved
    assert result["issues"][0]["labels"] == ["wave-0"]
    assert result["issues"][0]["state"] == {"name": "Backlog"}


@pytest.mark.asyncio
async def test_linear_list_project_issues_honors_limit(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_fake")
    _install_urlopen_queue(monkeypatch, [
        {"data": {"project": {
            "id": "proj-uuid",
            "name": "x",
            "issues": {
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                "nodes": [
                    {
                        "id": f"u{i}",
                        "identifier": f"SAL-{i}",
                        "title": "t",
                        "estimate": None,
                        "labels": {"nodes": []},
                        "state": {"name": "Todo"},
                        "relations": {"nodes": []},
                    }
                    for i in range(1, 6)
                ],
            },
        }}},
    ])
    result = await linear_list_project_issues("proj-uuid", limit=3)
    assert result["total"] == 3
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_linear_get_issue_relations_separates_by_type(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_fake")
    _install_urlopen_queue(monkeypatch, [
        {"data": {"issue": {
            "id": "uuid-a",
            "identifier": "SAL-100",
            "relations": {"nodes": [
                {"type": "blocks", "relatedIssue": {
                    "id": "uuid-b", "identifier": "SAL-101", "state": {"name": "Todo"},
                }},
                {"type": "blocks", "relatedIssue": {
                    "id": "uuid-c", "identifier": "SAL-102", "state": {"name": "Backlog"},
                }},
                {"type": "blocked_by", "relatedIssue": {
                    "id": "uuid-d", "identifier": "SAL-50", "state": {"name": "In Progress"},
                }},
                {"type": "related", "relatedIssue": {
                    "id": "uuid-e", "identifier": "SAL-200", "state": {"name": "Done"},
                }},
                {"type": "duplicate", "relatedIssue": {
                    "id": "uuid-f", "identifier": "SAL-999", "state": {"name": "Canceled"},
                }},
            ]},
        }}},
    ])
    result = await linear_get_issue_relations("SAL-100")
    assert result["identifier"] == "SAL-100"
    assert sorted(result["blocks"]) == ["SAL-101", "SAL-102"]
    assert result["blocked_by"] == ["SAL-50"]
    # `related` bucket includes both the explicit "related" and the soft-linked
    # "duplicate" type.
    assert sorted(result["related"]) == ["SAL-200", "SAL-999"]


@pytest.mark.asyncio
async def test_linear_get_issue_relations_missing_key(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("ALFRED_OPS_LINEAR_API_KEY", raising=False)
    result = await linear_get_issue_relations("SAL-100")
    assert "error" in result
    assert "LINEAR_API_KEY" in result["error"]
