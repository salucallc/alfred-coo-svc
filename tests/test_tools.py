"""Tool registry + handler tests.

HTTP-hitting handlers (linear_create_issue, slack_post) are exercised via their
error paths — missing credentials — which is all we can deterministically test
without network. End-to-end tool invocation is covered in the B.3 smoke-test
task after deployment.
"""

import asyncio
import json
import os
import re

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


# ── AB-10: github_merge_pr ──────────────────────────────────────────────────

from alfred_coo.tools import github_merge_pr


@pytest.mark.asyncio
async def test_github_merge_pr_missing_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = await github_merge_pr(
        owner="salucallc",
        repo="alfred-coo-svc",
        pr_number=1,
    )
    assert result == {"error": "missing GITHUB_TOKEN"}


@pytest.mark.asyncio
async def test_github_merge_pr_bad_owner(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    result = await github_merge_pr(
        owner="evil-org",
        repo="hack",
        pr_number=1,
    )
    assert "error" in result
    assert "allowlist" in result["error"]


@pytest.mark.asyncio
async def test_github_merge_pr_success(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    captured = _install_urlopen_queue(
        monkeypatch,
        [{"merged": True, "sha": "abc", "message": "Pull Request successfully merged"}],
    )
    result = await github_merge_pr(
        owner="salucallc",
        repo="alfred-coo-svc",
        pr_number=42,
        merge_method="squash",
    )
    assert result == {
        "ok": True,
        "merged": True,
        "sha": "abc",
        "message": "Pull Request successfully merged",
    }
    # Request should be a PUT to the merge endpoint with squash body.
    assert len(captured) == 1
    req = captured[0]
    assert req.get_method() == "PUT"
    assert req.full_url.endswith("/repos/salucallc/alfred-coo-svc/pulls/42/merge")
    body = json.loads(req.data.decode("utf-8"))
    assert body == {"merge_method": "squash"}


@pytest.mark.asyncio
async def test_github_merge_pr_not_mergeable_405(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")

    import io
    import urllib.error as _ue
    import urllib.request as _ur

    def fake_urlopen(req, timeout=None):
        raise _ue.HTTPError(
            req.full_url,
            405,
            "Method Not Allowed",
            {},
            io.BytesIO(b'{"message":"Pull Request is not mergeable"}'),
        )

    monkeypatch.setattr(_ur, "urlopen", fake_urlopen)

    result = await github_merge_pr(
        owner="salucallc",
        repo="alfred-coo-svc",
        pr_number=1,
    )
    assert result["error"] == "not_mergeable"
    assert result["status"] == 405
    assert "not mergeable" in result["body"].lower()


@pytest.mark.asyncio
async def test_github_merge_pr_stale_head_409(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")

    import io
    import urllib.error as _ue
    import urllib.request as _ur

    def fake_urlopen(req, timeout=None):
        raise _ue.HTTPError(
            req.full_url,
            409,
            "Conflict",
            {},
            io.BytesIO(b'{"message":"Head branch was modified. Review and try the merge again."}'),
        )

    monkeypatch.setattr(_ur, "urlopen", fake_urlopen)

    result = await github_merge_pr(
        owner="salucallc",
        repo="alfred-coo-svc",
        pr_number=1,
    )
    assert result["error"] == "stale_head"
    assert result["status"] == 409
    assert "head branch" in result["body"].lower()


def test_github_merge_pr_registered_in_builtins():
    assert "github_merge_pr" in BUILTIN_TOOLS
    spec = BUILTIN_TOOLS["github_merge_pr"]
    assert spec.handler is github_merge_pr
    assert spec.name == "github_merge_pr"
    schema = openai_tool_schema(spec)
    required = schema["function"]["parameters"]["required"]
    for key in ("owner", "repo", "pr_number"):
        assert key in required
    props = schema["function"]["parameters"]["properties"]
    assert props["merge_method"]["enum"] == ["squash", "merge", "rebase"]


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


# ── SAL-2890 Fix E: relaxed matcher (threaded + single-pending) ───────────


@pytest.mark.asyncio
async def test_slack_ack_poll_strict_still_matches_with_ss08_token(monkeypatch):
    """Non-threaded message with body=`approve SS-08` → still matches via
    the original AB-03 strict regex. No regression.
    """
    monkeypatch.setenv("SLACK_BOT_TOKEN_ALFRED", "xoxb-fake")
    _install_urlopen_queue(monkeypatch, [{
        "ok": True,
        "has_more": False,
        "messages": [
            {"ts": "150.0", "user": "U1", "text": "approve SS-08"},
        ],
    }])
    result = await slack_ack_poll(
        channel="C0ASAKFTR1C",
        after_ts="100.0",
        author_user_id="U1",
        keywords=[r"(ack|approve(d)?)\s*ss[-_\s]?08"],
        # Default relaxed=False — strict-only should still hit.
    )
    assert result["matched"] is True
    assert result.get("via") == "strict"


@pytest.mark.asyncio
async def test_slack_ack_poll_threaded_relaxed_matches_approved(monkeypatch):
    """Threaded reply (`thread_ts == gate_post_ts`) with body=`approved` →
    matches under relaxed mode despite missing the SS-08 token.
    """
    monkeypatch.setenv("SLACK_BOT_TOKEN_ALFRED", "xoxb-fake")
    # First call: conversations.replies (thread fetch). Returns the gate
    # parent + a threaded reply with bare `approved`.
    # Second call: conversations.history (channel scan). No matches.
    _install_urlopen_queue(monkeypatch, [
        {
            "ok": True,
            "has_more": False,
            "messages": [
                # Parent (gate post itself) — must be skipped.
                {"ts": "1000.0", "user": "Ubot", "text": "GATE: SS-08 schema..."},
                # Threaded reply from Cristian.
                {"ts": "1100.0", "user": "U1", "text": "approved",
                 "thread_ts": "1000.0"},
            ],
        },
        {
            "ok": True,
            "has_more": False,
            "messages": [],
        },
    ])
    result = await slack_ack_poll(
        channel="C0ASAKFTR1C",
        after_ts="999.0",
        author_user_id="U1",
        keywords=[r"(ack|approve(d)?)\s*ss[-_\s]?08"],  # strict won't match
        gate_post_ts="1000.0",
        relaxed=True,
        single_pending=False,
    )
    assert result["matched"] is True
    assert result["message_ts"] == "1100.0"
    assert result.get("via") == "thread"


@pytest.mark.asyncio
async def test_slack_ack_poll_threaded_relaxed_matches_thumbsup(monkeypatch):
    """Threaded reply with body=`👍` → matches under relaxed mode."""
    monkeypatch.setenv("SLACK_BOT_TOKEN_ALFRED", "xoxb-fake")
    _install_urlopen_queue(monkeypatch, [
        {
            "ok": True,
            "has_more": False,
            "messages": [
                {"ts": "1000.0", "user": "Ubot", "text": "GATE: SS-08 schema"},
                {"ts": "1100.0", "user": "U1", "text": "👍",
                 "thread_ts": "1000.0"},
            ],
        },
        {"ok": True, "has_more": False, "messages": []},
    ])
    result = await slack_ack_poll(
        channel="C0ASAKFTR1C",
        after_ts="999.0",
        author_user_id="U1",
        keywords=[r"(ack|approve(d)?)\s*ss[-_\s]?08"],
        gate_post_ts="1000.0",
        relaxed=True,
    )
    assert result["matched"] is True
    assert result["message_ts"] == "1100.0"
    assert result.get("via") == "thread"


@pytest.mark.asyncio
async def test_slack_ack_poll_relaxed_off_rejects_threaded_short_form(
    monkeypatch,
):
    """Default `relaxed=False`: a threaded `approved` does NOT match.
    The strict regex is the only authority.
    """
    monkeypatch.setenv("SLACK_BOT_TOKEN_ALFRED", "xoxb-fake")
    _install_urlopen_queue(monkeypatch, [{
        "ok": True,
        "has_more": False,
        "messages": [
            {"ts": "1100.0", "user": "U1", "text": "approved",
             "thread_ts": "1000.0"},
        ],
    }])
    result = await slack_ack_poll(
        channel="C0ASAKFTR1C",
        after_ts="999.0",
        author_user_id="U1",
        keywords=[r"(ack|approve(d)?)\s*ss[-_\s]?08"],  # requires SS-08 token
        # No gate_post_ts, no relaxed → no thread fetch, strict-only.
    )
    assert result == {"matched": False}


@pytest.mark.asyncio
async def test_slack_ack_poll_single_pending_matches_non_threaded_approved(
    monkeypatch,
):
    """`single_pending=True` + `relaxed=True` → bare "approved" in the
    main channel (not threaded) matches because we know the ACK target
    is unambiguous.
    """
    monkeypatch.setenv("SLACK_BOT_TOKEN_ALFRED", "xoxb-fake")
    _install_urlopen_queue(monkeypatch, [{
        "ok": True,
        "has_more": False,
        "messages": [
            {"ts": "150.0", "user": "U1", "text": "approved"},
        ],
    }])
    result = await slack_ack_poll(
        channel="C0ASAKFTR1C",
        after_ts="100.0",
        author_user_id="U1",
        keywords=[r"(ack|approve(d)?)\s*ss[-_\s]?08"],  # strict won't match
        relaxed=True,
        single_pending=True,
    )
    assert result["matched"] is True
    assert result.get("via") == "single_pending"


@pytest.mark.asyncio
async def test_slack_ack_poll_relaxed_without_guards_rejects_short_form(
    monkeypatch,
):
    """Spec acceptance: non-threaded message with body=`approved` and 2
    gates pending (i.e. `single_pending=False`) → does NOT match. The
    single-gate-inference safety must hold.
    """
    monkeypatch.setenv("SLACK_BOT_TOKEN_ALFRED", "xoxb-fake")
    _install_urlopen_queue(monkeypatch, [{
        "ok": True,
        "has_more": False,
        "messages": [
            {"ts": "150.0", "user": "U1", "text": "approved"},
        ],
    }])
    result = await slack_ack_poll(
        channel="C0ASAKFTR1C",
        after_ts="100.0",
        author_user_id="U1",
        keywords=[r"(ack|approve(d)?)\s*ss[-_\s]?08"],
        relaxed=True,
        single_pending=False,  # multi-gate world
        # No gate_post_ts → no thread context either.
    )
    assert result == {"matched": False}


@pytest.mark.asyncio
async def test_slack_ack_poll_thread_call_uses_replies_endpoint(monkeypatch):
    """When `gate_post_ts` is supplied, the FIRST HTTP call must hit
    `conversations.replies` (not `conversations.history`).
    """
    monkeypatch.setenv("SLACK_BOT_TOKEN_ALFRED", "xoxb-fake")
    captured = _install_urlopen_queue(monkeypatch, [
        # Replies fetch — empty, so we fall through to history.
        {"ok": True, "has_more": False, "messages": []},
        # History scan — also empty.
        {"ok": True, "has_more": False, "messages": []},
    ])
    result = await slack_ack_poll(
        channel="C0ASAKFTR1C",
        after_ts="999.0",
        author_user_id="U1",
        keywords=[r"ack"],
        gate_post_ts="1000.0",
        relaxed=True,
    )
    assert result == {"matched": False}
    assert len(captured) == 2
    assert "conversations.replies" in captured[0].full_url
    assert "ts=1000.0" in captured[0].full_url
    assert "conversations.history" in captured[1].full_url


@pytest.mark.asyncio
async def test_slack_ack_poll_strict_still_wins_over_relaxed(monkeypatch):
    """When the strict regex matches, the result reports `via="strict"`
    even with relaxed=True. Strict is the canonical path.
    """
    monkeypatch.setenv("SLACK_BOT_TOKEN_ALFRED", "xoxb-fake")
    _install_urlopen_queue(monkeypatch, [{
        "ok": True,
        "has_more": False,
        "messages": [
            {"ts": "150.0", "user": "U1", "text": "ACK SS-08 go"},
        ],
    }])
    result = await slack_ack_poll(
        channel="C0ASAKFTR1C",
        after_ts="100.0",
        author_user_id="U1",
        keywords=[r"(ack|approve(d)?)\s*ss[-_\s]?08"],
        relaxed=True,
        single_pending=True,
    )
    assert result["matched"] is True
    assert result.get("via") == "strict"


@pytest.mark.asyncio
async def test_slack_ack_poll_thread_skips_parent_message(monkeypatch):
    """The gate parent post (msg.ts == gate_post_ts) MUST NOT be matched
    even if its text contains "approved" — replies only.
    """
    monkeypatch.setenv("SLACK_BOT_TOKEN_ALFRED", "xoxb-fake")
    _install_urlopen_queue(monkeypatch, [
        {
            "ok": True,
            "has_more": False,
            "messages": [
                # Parent (text would relaxed-match if not skipped).
                {"ts": "1000.0", "user": "U1",
                 "text": "GATE: SS-08 — approved schema attached"},
            ],
        },
        {"ok": True, "has_more": False, "messages": []},
    ])
    result = await slack_ack_poll(
        channel="C0ASAKFTR1C",
        after_ts="999.0",
        author_user_id="U1",
        keywords=[r"(ack|approve(d)?)\s*ss[-_\s]?08"],  # strict won't match
        gate_post_ts="1000.0",
        relaxed=True,
        single_pending=False,
    )
    assert result == {"matched": False}


@pytest.mark.asyncio
async def test_slack_ack_poll_schema_includes_relaxed_fields():
    """The new optional fields must surface in the OpenAI tool schema."""
    schema = openai_tool_schema(BUILTIN_TOOLS["slack_ack_poll"])
    props = schema["function"]["parameters"]["properties"]
    for key in ("gate_post_ts", "relaxed", "single_pending"):
        assert key in props, f"missing optional schema property {key!r}"
    # Original required set unchanged.
    required = schema["function"]["parameters"]["required"]
    assert set(required) == {"channel", "after_ts", "author_user_id", "keywords"}


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


# ── AB-17-o: update_pr tool ────────────────────────────────────────────────
#
# v8-full-v4 (mesh task 83dd216d, 2026-04-24 ~18:14 UTC) surfaced the
# duplicate-PR leak: after AB-17-k fixed respawn grounding, fix-round
# children call ``propose_pr`` with a fresh timestamped branch → a NEW PR
# per REQUEST_CHANGES cycle (wave-0: acs#59/60, ts#4/5, ss#17/18). AB-17-o
# adds ``update_pr`` to push to the existing branch instead.

from alfred_coo.tools import update_pr


def test_update_pr_registered():
    assert "update_pr" in BUILTIN_TOOLS
    schema = openai_tool_schema(BUILTIN_TOOLS["update_pr"])
    required = schema["function"]["parameters"]["required"]
    for key in ("pr_url", "branch", "commit_message", "files"):
        assert key in required, f"update_pr schema missing required field: {key}"


@pytest.mark.asyncio
async def test_update_pr_pushes_to_existing_branch(monkeypatch, tmp_path):
    """Happy path: update_pr clones, fetches the branch, writes files,
    commits, and pushes to the SAME branch. Return envelope carries the
    pushed sha + commit_url.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    monkeypatch.setenv("ALFRED_WORKSPACES_ROOT", str(tmp_path))
    # WORKSPACE_ROOT is resolved at import time from the env var, so rebind
    # it to the tmp path too.
    import alfred_coo.tools as t
    monkeypatch.setattr(t, "WORKSPACE_ROOT", tmp_path)

    # Stub the PR-meta GET to return an open PR on branch "feature/sal-2615-x".
    class _Resp:
        def __init__(self, body):
            self._body = body.encode() if isinstance(body, str) else body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._body

    def _fake_urlopen(req, timeout=30):
        method = getattr(req, "method", None) or "GET"
        if method == "GET" and "/pulls/" in req.full_url:
            return _Resp(json.dumps({
                "state": "open", "merged": False,
                "head": {"ref": "feature/sal-2615-x"},
            }))
        # No title/body patch in this test, but leave a no-op just in case.
        return _Resp(json.dumps({}))

    monkeypatch.setattr(t.urllib.request, "urlopen", _fake_urlopen)

    # Stub _run to record git calls and synthesise successful outputs.
    calls: list[list[str]] = []
    async def _fake_run(cmd, cwd=None, env=None):
        calls.append(list(cmd))
        # Simulate a real clone by creating the workspace directory so
        # subsequent file writes succeed.
        if cmd[:2] == ["git", "clone"]:
            import pathlib
            pathlib.Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
        if cmd[:2] == ["git", "rev-parse"]:
            return 0, "deadbeefcafefeed1234\n", ""
        return 0, "", ""
    monkeypatch.setattr(t, "_run", _fake_run)

    result = await update_pr(
        pr_url="https://github.com/salucallc/alfred-coo-svc/pull/59",
        branch="feature/sal-2615-x",
        commit_message="fix(sal-2615): address review feedback",
        files=[{"path": "src/foo.py", "content": "print('ok')\n"}],
    )

    assert "error" not in result, f"unexpected error: {result}"
    assert result["pushed_sha"].startswith("deadbeefcafe")
    assert result["pr_url"] == "https://github.com/salucallc/alfred-coo-svc/pull/59"
    assert result["branch"] == "feature/sal-2615-x"
    assert result["pr_number"] == 59
    assert result["commit_url"].endswith("/commit/deadbeefcafefeed1234")

    cmd_strs = [" ".join(c) for c in calls]
    joined = " | ".join(cmd_strs)
    assert any(c.startswith("git clone") for c in cmd_strs), joined
    assert any(
        c == "git fetch origin feature/sal-2615-x" for c in cmd_strs
    ), joined
    assert any(
        c == "git checkout -B feature/sal-2615-x origin/feature/sal-2615-x"
        for c in cmd_strs
    ), joined
    assert any(
        c == "git push origin feature/sal-2615-x" for c in cmd_strs
    ), joined
    # Crucially: the push is to the EXISTING branch, not a new one.
    assert not any("propose" in c for c in cmd_strs)


@pytest.mark.asyncio
async def test_update_pr_rejects_closed_pr(monkeypatch, tmp_path):
    """update_pr must refuse to touch a CLOSED PR. A fix-round on a
    closed PR is a caller bug; silently pushing would leave the branch
    out of date with no review thread watching."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    import alfred_coo.tools as t

    class _Resp:
        def __init__(self, body):
            self._body = body.encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._body

    def _fake_urlopen(req, timeout=30):
        return _Resp(json.dumps({
            "state": "closed", "merged": False,
            "head": {"ref": "feature/sal-2615-x"},
        }))

    monkeypatch.setattr(t.urllib.request, "urlopen", _fake_urlopen)

    result = await update_pr(
        pr_url="https://github.com/salucallc/alfred-coo-svc/pull/59",
        branch="feature/sal-2615-x",
        commit_message="fix: x",
        files=[{"path": "a.py", "content": "x"}],
    )
    assert "error" in result
    assert "state=closed" in result["error"]


@pytest.mark.asyncio
async def test_update_pr_rejects_missing_branch(monkeypatch, tmp_path):
    """If the feature branch no longer exists on origin, update_pr must
    bail with a clear error rather than falling back to creating a new
    branch (that is propose_pr's job)."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    monkeypatch.setenv("ALFRED_WORKSPACES_ROOT", str(tmp_path))
    import alfred_coo.tools as t
    monkeypatch.setattr(t, "WORKSPACE_ROOT", tmp_path)

    class _Resp:
        def __init__(self, body):
            self._body = body.encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._body

    def _fake_urlopen(req, timeout=30):
        return _Resp(json.dumps({
            "state": "open", "merged": False,
            "head": {"ref": "feature/sal-2615-x"},
        }))
    monkeypatch.setattr(t.urllib.request, "urlopen", _fake_urlopen)

    async def _fake_run(cmd, cwd=None, env=None):
        if cmd[:2] == ["git", "clone"]:
            import pathlib
            pathlib.Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
            return 0, "", ""
        if cmd[:2] == ["git", "fetch"]:
            return 1, "", (
                "fatal: couldn't find remote ref feature/sal-2615-x"
            )
        return 0, "", ""
    monkeypatch.setattr(t, "_run", _fake_run)

    result = await update_pr(
        pr_url="https://github.com/salucallc/alfred-coo-svc/pull/59",
        branch="feature/sal-2615-x",
        commit_message="fix: x",
        files=[{"path": "a.py", "content": "x"}],
    )
    assert "error" in result
    assert "branch not found" in result["error"]


@pytest.mark.asyncio
async def test_update_pr_rejects_empty_files(monkeypatch):
    """An empty files list should NOT silently succeed — that would hide
    a bug in the caller (fix-round with nothing to change)."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    result = await update_pr(
        pr_url="https://github.com/salucallc/alfred-coo-svc/pull/59",
        branch="feature/sal-2615-x",
        commit_message="fix: x",
        files=[],
    )
    assert "error" in result
    assert "files" in result["error"]


@pytest.mark.asyncio
async def test_update_pr_rejects_non_saluca_owner(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    result = await update_pr(
        pr_url="https://github.com/evil-org/hack/pull/1",
        branch="feature/x",
        commit_message="x",
        files=[{"path": "a", "content": "b"}],
    )
    assert "error" in result
    assert "allowlist" in result["error"]


# ── SAL-2953: APE/V citation auto-inject ────────────────────────────────────
#
# v7y wave-1 burned 3 hawkman review cycles across 2 dispatched tickets
# because the builder's PR body forgot the `## APE/V Citation` heading
# even though the persona instructions are explicit. The orchestrator-side
# fix is deterministic: at propose_pr / update_pr time, synthesise the
# citation block from the plan doc the builder already ships in `files`.
#
# These tests cover three properties:
#  1. Builder bodies WITHOUT a citation get one auto-injected from the
#     plan doc in `files` (acceptance criteria preserved verbatim).
#  2. The injected block matches what hawkman gate-3 / gate-1 looks for
#     (the canonical shape from acs#96 / tir#8: `## APE/V Citation`
#     heading, plan-doc-path bullet, verification bullet, fenced
#     acceptance-criteria block).
#  3. Builder bodies that ALREADY carry a citation are returned
#     unchanged — the helper does not duplicate.

from alfred_coo.tools import (
    _apev_body_has_citation,
    _build_apev_citation_block,
    _extract_acceptance_lines,
    _extract_ticket_code,
    _find_plan_doc_in_files,
    _maybe_inject_apev_citation,
)


_SAMPLE_PLAN_DOC = """# SAL-2953: auto-inject APE/V citation

## Target paths
- src/alfred_coo/tools.py

## Acceptance criteria
- New PRs from builder persona include an `## APE/V` (or equivalent) section.
- The block contains the fields hawkman currently checks for.
- Idempotent: existing citations are not duplicated.

## Verification approach
Unit tests in tests/test_tools.py.

## Risks
- Block format drift vs. hawkman parser.
"""


def test_apev_helper_extracts_ticket_code_from_branch():
    assert _extract_ticket_code("feature/sal-2953-x") == "SAL-2953"
    assert _extract_ticket_code(None, "SAL-2611: do thing") == "SAL-2611"
    assert _extract_ticket_code("no-code-here") is None


def test_apev_helper_finds_plan_doc_by_ticket_code():
    files = {
        "src/foo.py": "x = 1\n",
        "plans/v1-ga/SAL-2953.md": _SAMPLE_PLAN_DOC,
    }
    path, content = _find_plan_doc_in_files(files, ticket_code="SAL-2953")
    assert path == "plans/v1-ga/SAL-2953.md"
    assert content == _SAMPLE_PLAN_DOC


def test_apev_helper_extracts_acceptance_lines():
    lines = _extract_acceptance_lines(_SAMPLE_PLAN_DOC)
    assert lines is not None
    assert "auto-injected from the plan doc" in lines or "auto-injected" in lines or (
        "include an `## APE/V`" in lines
    )
    # Subsequent sections must NOT be folded in.
    assert "## Verification approach" not in lines
    assert "## Risks" not in lines


def test_apev_body_has_citation_detects_canonical_heading():
    canonical = (
        "## APE/V Citation\n- Plan doc path: `plans/v1-ga/SAL-2611.md`\n"
    )
    assert _apev_body_has_citation(canonical) is True
    # Variants the builder might emit:
    assert _apev_body_has_citation("## APE-V\n") is True
    assert _apev_body_has_citation("### APE/V Citation\n") is True
    assert _apev_body_has_citation("# APE/V\n") is False  # h1 is not the gate
    assert _apev_body_has_citation("## Acceptance criteria\n") is False
    assert _apev_body_has_citation("") is False
    assert _apev_body_has_citation(None) is False


def test_builder_pr_body_includes_apev_citation():
    """Given a builder result missing the citation heading + a plan doc
    in files, the assembled body MUST contain the `## APE/V Citation`
    section with the plan-doc path bullet."""
    builder_body = (
        "Implements the SAL-2953 fix.\n\n"
        "## Summary\n- Added auto-inject helper.\n"
    )
    files = {
        "src/alfred_coo/tools.py": "# patch\n",
        "plans/v1-ga/SAL-2953.md": _SAMPLE_PLAN_DOC,
    }
    out = _maybe_inject_apev_citation(
        builder_body,
        files=files,
        branch="fix/SAL-2953-builder-pr-template-apev-autoinject",
        title="SAL-2953: auto-inject APE/V citation in builder PR template",
    )
    assert "## APE/V Citation" in out
    assert "plans/v1-ga/SAL-2953.md" in out
    # Original content preserved.
    assert "Implements the SAL-2953 fix." in out
    assert "## Summary" in out
    # Verbatim acceptance lines from the plan doc end up in the block.
    assert "Idempotent: existing citations are not duplicated." in out


def test_builder_pr_body_apev_format_matches_hawkman_expectation():
    """Feed the assembled body to a parser that mimics hawkman GATE 1 +
    GATE 3 (the same regex shape the hawkman LLM persona looks for in
    persona.py) and assert the body APPROVES, not REQUEST_CHANGES.

    Hawkman GATE 1 (persona.py L344-351): "Look for a fenced block or
    quoted paragraph that reproduces the A/P/E/V acceptance criteria
    from the plan doc." The canonical shape used by APPROVED PRs
    acs#96 + tir#8:

        ## APE/V Citation
        - Plan doc path: `plans/v1-ga/<TICKET>.md`
        - Verification: <line>
        - Acceptance criteria:
        ```
        <verbatim>
        ```
    """
    builder_body = "Patch text.\n"
    files = {"plans/v1-ga/SAL-2953.md": _SAMPLE_PLAN_DOC}
    out = _maybe_inject_apev_citation(
        builder_body,
        files=files,
        branch="fix/sal-2953-x",
    )

    # Hawkman-shaped checks (mirrors what an LLM reviewer grep-matches):
    # 1. The heading is present (GATE 1: APE/V citation present).
    import re as _re
    heading_re = _re.compile(r"(?im)^#{2,3}\s+APE\s*/\s*V")
    assert heading_re.search(out), f"missing APE/V heading: {out!r}"
    # 2. A plan-doc path bullet is present (GATE 3: target paths grounded).
    plan_doc_re = _re.compile(
        r"(?im)^[\-*]\s+Plan doc(?:\s+path)?:\s*`plans/v1-ga/[A-Za-z0-9_-]+\.md`"
    )
    assert plan_doc_re.search(out), f"missing plan-doc path bullet: {out!r}"
    # 3. A fenced or quoted block reproducing the acceptance criteria.
    fence_re = _re.compile(r"```[\s\S]*?Idempotent:[\s\S]*?```")
    assert fence_re.search(out), (
        f"missing fenced acceptance-criteria block: {out!r}"
    )
    # 4. A verification bullet (the third canonical bullet on ac#96).
    verif_re = _re.compile(r"(?im)^[\-*]\s+Verification:")
    assert verif_re.search(out), f"missing Verification bullet: {out!r}"


def test_builder_pr_body_does_not_double_inject_apev():
    """If the builder LLM already wrote a citation block, the helper
    must return the body UNCHANGED — no duplicate heading, no duplicate
    fenced block. Idempotency is the contract that lets the orchestrator
    inject without fighting builders that did the right thing.
    """
    canonical_body = (
        "Implements SAL-2611.\n\n"
        "## APE/V Citation\n"
        "- Plan doc path: `plans/v1-ga/SAL-2611.md`\n"
        "- Verification: CLI prints token; DB row created; TTL enforced.\n"
        "- Acceptance criteria:\n"
        "```\n"
        "`mcctl token create --site acme-sfo --ttl 15m` prints one-shot\n"
        "```\n"
    )
    files = {"plans/v1-ga/SAL-2611.md": _SAMPLE_PLAN_DOC}
    out = _maybe_inject_apev_citation(
        canonical_body,
        files=files,
        branch="feature/sal-2611-mcctl",
    )
    # Exact equality: no characters added, no characters removed.
    assert out == canonical_body
    # And — belt and braces — there is exactly one heading.
    assert out.count("## APE/V Citation") == 1


def test_apev_inject_falls_back_to_ticket_code_when_no_plan_doc():
    """When the builder forgot BOTH the plan doc and the citation, the
    helper still inserts a citation block so hawkman GATE 1 sees a
    heading + plan-doc-path bullet derived from the branch name. This
    is the worst-case fallback — better a stub citation pointing at the
    expected plan-doc path than nothing at all."""
    out = _maybe_inject_apev_citation(
        "no plan doc, no citation",
        files={"src/foo.py": "x = 1\n"},
        branch="fix/sal-2999-no-plan",
    )
    assert "## APE/V Citation" in out
    assert "plans/v1-ga/SAL-2999.md" in out


def test_apev_inject_handles_empty_body_and_files():
    """Defensive: empty body + empty files must not crash and must still
    produce a citation block (with placeholder fields) so the helper
    never returns an APE/V-less body downstream of a builder that
    emitted essentially nothing."""
    out = _maybe_inject_apev_citation(
        "",
        files=None,
        branch="feature/sal-2953",
    )
    assert "## APE/V Citation" in out
    assert "SAL-2953" in out


def test_build_apev_block_canonical_shape():
    """Direct test of the block builder: the produced block must carry
    the four fields hawkman gate-1 expects, in the order the APPROVED
    PRs ac#96 / tir#8 used."""
    block = _build_apev_citation_block(
        plan_doc_path="plans/v1-ga/SAL-2611.md",
        acceptance_lines="One-shot token printed; DB row created.",
        verification="CLI test green.",
        ticket_code="SAL-2611",
    )
    # Heading first, then the three bullets in canonical order.
    assert block.lstrip().startswith("## APE/V Citation\n")
    assert "- Plan doc path: `plans/v1-ga/SAL-2611.md`" in block
    assert "- Verification: CLI test green." in block
    assert "- Acceptance criteria:" in block
    assert "```\nOne-shot token printed; DB row created.\n```" in block


# ── SAL-2965: fenced-format regression, Linear source, fix-round skip ───────
#
# v7z observed two distinct mismatches between the SAL-2953 auto-inject
# and hawkman's gate-1 expectations:
#   1. Source: plan doc is builder-authored and drifts on fix-rounds
#      (PR #37 SAL-2613: 5 cycles where the fix-round directive was
#      extracted as acceptance criteria). Linear ticket body is
#      canonical and what hawkman validates against.
#   2. Fix-round: update_pr re-running the auto-inject overwrote the
#      original (clean) PR body with text re-extracted from a drifted
#      plan doc.
# The SAL-2965 fix:
#   - Linear ticket body is the primary source; plan doc is fallback.
#   - update_pr passes is_fix_round=True; the helper short-circuits.
#   - Format remains the canonical fenced acceptance block (matches
#     PR #38 SAL-2665 and PR #35 SAL-2610 hawkman-APPROVED exemplars).


def test_builder_pr_body_apev_uses_fenced_acceptance():
    """Regression: SAL-2965 ticket reported "bullets vs fenced". The
    auto-inject MUST emit the acceptance criteria inside a triple-
    backtick fenced block (verbatim), matching the PR #38 SAL-2665 and
    PR #35 SAL-2610 hawkman-APPROVED exemplars. Hawkman gate-1 grep-
    matches on the fenced block, not on a bare bullet line.
    """
    builder_body = "Patch text only; no citation.\n"
    files = {"plans/v1-ga/SAL-2965.md": _SAMPLE_PLAN_DOC}
    out = _maybe_inject_apev_citation(
        builder_body,
        files=files,
        branch="fix/SAL-2965-x",
        # Force the plan-doc fallback (no Linear) for this test by
        # passing a fetcher that returns None.
        linear_fetcher=lambda code: None,
    )
    # Must contain a triple-backtick fenced block enclosing the verbatim
    # acceptance lines from the plan doc.
    fence_re = re.compile(
        r"```\n[\s\S]*?Idempotent: existing citations are not duplicated\."
        r"[\s\S]*?\n```"
    )
    assert fence_re.search(out), (
        f"acceptance criteria must be in a fenced code block, got: {out!r}"
    )
    # Sanity-check the fence open / close are balanced (hawkman parser
    # would reject a stray opening fence with no close): there must be
    # an even number of triple-backtick markers.
    assert out.count("```") % 2 == 0, (
        f"unbalanced fence markers in body: {out!r}"
    )
    # The fenced block must directly follow the `- Acceptance criteria:`
    # bullet (the canonical PR #38 / PR #35 shape: bullet line, then
    # opening fence on the next non-empty line).
    canonical_shape_re = re.compile(
        r"-\s+Acceptance criteria:\s*\n```\n", re.MULTILINE
    )
    assert canonical_shape_re.search(out), (
        f"expected `- Acceptance criteria:` bullet immediately followed "
        f"by an opening fence, got: {out!r}"
    )


def test_builder_pr_body_apev_extracts_from_linear_not_plan_doc():
    """SAL-2965 source change: when Linear has the canonical ticket body
    and the plan doc has drifted, the auto-injected block MUST quote the
    Linear text, not the plan-doc text. This is the bug hawkman caught
    on soul-svc#37 SAL-2613 (auto-inject shipped the fix-round directive
    as acceptance criteria, since the plan doc had been overwritten).
    """
    plan_doc_drifted = (
        "# SAL-2613: drifted plan doc\n\n"
        "## Acceptance criteria\n"
        "- [ ] Address every point in the review feedback below.\n"
        "- [ ] Push fixes to the EXISTING branch via update_pr.\n\n"
        "## Verification approach\n"
        "Re-run review.\n"
    )
    canonical_linear_body = (
        "ack p95 <500ms local; 3 missed -> mode_state=degraded; "
        "visible in /v1/fleet/endpoints/{id}"
    )

    files = {"plans/v1-ga/SAL-2613.md": plan_doc_drifted}
    out = _maybe_inject_apev_citation(
        "no citation here",
        files=files,
        branch="feature/sal-2613-heartbeat",
        # Inject a fake Linear fetcher returning the canonical body.
        linear_fetcher=lambda code: (
            canonical_linear_body if code == "SAL-2613" else None
        ),
    )
    # The Linear (canonical) text must appear in the fenced block.
    assert "ack p95 <500ms local" in out
    assert "mode_state=degraded" in out
    # The drifted plan-doc text must NOT appear (would indicate the
    # helper preferred the wrong source).
    assert "Address every point in the review feedback" not in out
    assert "Push fixes to the EXISTING branch" not in out


def test_apev_falls_back_to_plan_doc_when_linear_unavailable():
    """When the Linear fetcher returns None (no API key, transport
    error, ticket missing) the helper must fall back to the plan-doc
    extraction so air-gapped tests + offline fixtures still get a
    deterministic citation block."""
    files = {"plans/v1-ga/SAL-2953.md": _SAMPLE_PLAN_DOC}
    out = _maybe_inject_apev_citation(
        "Patch only.\n",
        files=files,
        branch="fix/sal-2953-x",
        linear_fetcher=lambda code: None,  # Linear unreachable.
    )
    # Plan-doc acceptance line lands in the citation.
    assert "Idempotent: existing citations are not duplicated." in out
    assert "## APE/V Citation" in out


def test_update_pr_skips_apev_auto_inject():
    """SAL-2965 fix-round skip: update_pr passes is_fix_round=True so
    the helper returns the body unchanged regardless of whether a
    citation is present, regardless of plan-doc presence in files,
    regardless of the Linear fetch outcome. This preserves the
    original PR body's citation across fix-rounds and prevents the
    helper from clobbering it with text re-extracted from a drifted
    plan doc.
    """
    body_without_citation = "Body with no APE/V section at all.\n"
    files = {"plans/v1-ga/SAL-2613.md": _SAMPLE_PLAN_DOC}

    # Sentinel: even with a fetcher that WOULD return acceptance
    # criteria, fix-round skip means body is returned unchanged.
    def fetcher_would_return(code):
        return "this would be injected"

    out = _maybe_inject_apev_citation(
        body_without_citation,
        files=files,
        branch="feature/sal-2613-x",
        is_fix_round=True,
        linear_fetcher=fetcher_would_return,
    )
    assert out == body_without_citation
    assert "## APE/V Citation" not in out
    assert "this would be injected" not in out

    # And: a body that ALREADY has a citation also passes through
    # unchanged (which would be true even without the skip, but the
    # contract holds).
    body_with_citation = (
        "Original.\n\n## APE/V Citation\n"
        "- Plan doc path: `plans/v1-ga/SAL-2613.md`\n"
        "- Verification: original verification\n"
        "- Acceptance criteria:\n```\noriginal acceptance\n```\n"
    )
    out2 = _maybe_inject_apev_citation(
        body_with_citation,
        is_fix_round=True,
        linear_fetcher=fetcher_would_return,
    )
    assert out2 == body_with_citation


def test_update_pr_skips_apev_with_empty_body():
    """Edge: fix-round with empty body returns empty string (no inject,
    no crash). The fix-round contract is "do not touch the body";
    nothing-to-touch is still nothing-to-touch."""
    out = _maybe_inject_apev_citation(
        "",
        is_fix_round=True,
        linear_fetcher=lambda code: "would-be-acceptance",
    )
    assert out == ""


def test_fetch_linear_acceptance_criteria_no_key(monkeypatch):
    """Without LINEAR_API_KEY the production fetcher must return None
    cleanly so the caller falls back to plan-doc extraction."""
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("ALFRED_OPS_LINEAR_API_KEY", raising=False)
    from alfred_coo.tools import _fetch_linear_acceptance_criteria
    assert _fetch_linear_acceptance_criteria("SAL-2965") is None
    assert _fetch_linear_acceptance_criteria(None) is None
    assert _fetch_linear_acceptance_criteria("") is None


# ────────────────────────────────────────────────────────────────────────────
# SAL-2965 (post-evidence-2026-04-26): hawkman gate-1 verbatim contract.
#
# PR #103 (SAL-2601) shipped the *correct format* (## APE/V Citation +
# fenced block) but still got REQUEST_CHANGES because the citation
# *paraphrased* the Linear ticket body — semicolons → periods, tuples
# rewritten with backticks, "and green" dropped. Hawkman does a byte-
# verbatim substring match against the Linear ticket body, so any
# normalisation drifts the citation off-source and breaks the gate.
#
# These tests pin the helper to a no-rewrite contract.
# ────────────────────────────────────────────────────────────────────────────


def test_extract_acceptance_handles_apev_machinecheckable_heading():
    """Mission Control v1 GA tickets use ``## APE/V Acceptance (machine-
    checkable)`` (verified on SAL-2601, SAL-2613). The extractor must
    match this heading variant and return the body byte-verbatim — not
    the plan-doc-only ``## Acceptance criteria`` variant the SAL-2953
    extractor was built for.
    """
    from alfred_coo.tools import _extract_acceptance_lines

    sal2601_linear_description = (
        "**Epic:** B. Aletheia Daemon\n"
        "**Plan doc:** file:///Z:/_planning/v1-ga/B_aletheia_daemon.md\n"
        "**Ticket code:** SAL-ALT-04\n"
        "**Wave:** 1\n\n"
        "## APE/V Acceptance (machine-checkable)\n\n"
        "Given 12 (action_class, risk_tier) rows, router returns expected "
        "model_id; refuses when generator_model == candidate_verifier_model; "
        "unit tests committed and green\n\n"
        "## Effort\n\n"
        "S (estimate = 1 pts)\n"
    )
    out = _extract_acceptance_lines(sal2601_linear_description)
    assert out is not None, "must match `## APE/V Acceptance (machine-checkable)`"
    # Byte-for-byte preservation of the Linear body — semicolons stay
    # semicolons, tuples stay un-quoted, "and green" is preserved. The
    # trailing newline is trimmed (outer whitespace only).
    expected = (
        "Given 12 (action_class, risk_tier) rows, router returns expected "
        "model_id; refuses when generator_model == candidate_verifier_model; "
        "unit tests committed and green"
    )
    assert out == expected, (
        f"extracted text drifted from Linear source.\n"
        f"  expected: {expected!r}\n"
        f"  got:      {out!r}"
    )


def test_extract_acceptance_handles_apev_acceptance_no_parens():
    """Variant without the parenthetical: ``## APE/V Acceptance``."""
    from alfred_coo.tools import _extract_acceptance_lines

    src = (
        "## APE/V Acceptance\n"
        "Given X; refuses when Y; unit tests green\n\n"
        "## Effort\nS\n"
    )
    out = _extract_acceptance_lines(src)
    assert out == "Given X; refuses when Y; unit tests green"


def test_extract_acceptance_handles_legacy_acceptance_criteria():
    """Plan-doc / legacy ticket variant: ``## Acceptance criteria``."""
    from alfred_coo.tools import _extract_acceptance_lines

    src = (
        "## Acceptance criteria\n"
        "- foo; bar; baz (with semicolons preserved)\n"
        "- (tuple, like, this) preserved as-is\n\n"
        "## Verification\n"
    )
    out = _extract_acceptance_lines(src)
    expected = (
        "- foo; bar; baz (with semicolons preserved)\n"
        "- (tuple, like, this) preserved as-is"
    )
    assert out == expected


def test_extract_acceptance_preserves_semicolons_byte_verbatim():
    """Hawkman regression: PR #103 paraphrased semicolons to periods.
    The helper MUST NOT do that. Bytes in must equal bytes out (modulo
    outer whitespace trim only).
    """
    from alfred_coo.tools import _extract_acceptance_lines

    src = (
        "## APE/V Acceptance (machine-checkable)\n"
        "Foo; bar; baz\n"
        "## Next\n"
    )
    out = _extract_acceptance_lines(src)
    # Must NOT be rewritten to "Foo. Bar. Baz."
    assert out == "Foo; bar; baz"
    assert "." not in out, (
        "semicolons must not be rewritten to periods (PR #103 drift bug)"
    )


def test_apev_inject_quotes_linear_body_byte_verbatim():
    """End-to-end: when Linear returns the canonical ticket body, the
    auto-injected fenced block must contain that body byte-for-byte.
    Reproduces the PR #103 (SAL-2601) failure: the Linear text
    ``"... refuses when generator_model == candidate_verifier_model; unit
    tests committed and green"`` must land in the citation block exactly
    — no semicolon-to-period rewriting, no backtick wrapping of the
    ``(action_class, risk_tier)`` tuple, no dropping ``"and green"``.
    """
    canonical_linear_body = (
        "Given 12 (action_class, risk_tier) rows, router returns expected "
        "model_id; refuses when generator_model == candidate_verifier_model; "
        "unit tests committed and green"
    )
    out = _maybe_inject_apev_citation(
        "PR body without citation.\n",
        files={},
        branch="feature/sal-2601-router",
        linear_fetcher=lambda code: (
            canonical_linear_body if code == "SAL-2601" else None
        ),
    )
    # The exact Linear body string MUST appear unchanged in the output.
    assert canonical_linear_body in out, (
        f"verbatim Linear body missing from injected block.\n"
        f"  expected substring: {canonical_linear_body!r}\n"
        f"  actual body: {out!r}"
    )
    # Negative: none of the PR #103 paraphrase artefacts may appear.
    forbidden_paraphrases = [
        # PR #103 split semicolons into separate sentences with periods.
        "router returns expected model_id.",
        "Refuses when generator_model",
        # PR #103 wrapped the tuple in backticks.
        "`(action_class, risk_tier)`",
        # PR #103 dropped "and green".
        "unit tests committed.",
    ]
    for bad in forbidden_paraphrases:
        assert bad not in out, (
            f"injected body contains paraphrase artefact {bad!r} — "
            f"hawkman gate-1 will REQUEST_CHANGES.\n  body: {out!r}"
        )


def test_apev_inject_preserves_linear_body_with_special_chars():
    """Linear bodies in MC v1 GA tickets contain ``==``, ``<``, tuples,
    and multi-clause semicolon lists. None of these may be normalised.
    """
    src_body = (
        "ack p95 <500ms local; 3 missed -> mode_state=degraded; "
        "visible in /v1/fleet/endpoints/{id}"
    )
    out = _maybe_inject_apev_citation(
        "no citation",
        files={},
        branch="feature/sal-2613-heartbeat",
        linear_fetcher=lambda code: src_body,
    )
    assert src_body in out, (
        f"special-char body must be preserved verbatim.\n"
        f"  expected: {src_body!r}\n"
        f"  got body: {out!r}"
    )


def test_apev_inject_does_not_strip_paths_or_tuples():
    """Defensive: a body containing slashes, parens, asterisks, and
    backticks must round-trip unchanged through the helper.
    """
    src = (
        "Given inputs (a, b, c); calls /v1/foo/{id}; returns {\"ok\": true}; "
        "asserts `state == \"degraded\"` and emits *audit log* entry"
    )
    out = _maybe_inject_apev_citation(
        "x",
        files={},
        branch="feature/sal-9999-z",
        linear_fetcher=lambda code: src,
    )
    assert src in out, (
        f"complex body lost characters in transit.\n"
        f"  expected: {src!r}\n"
        f"  got:      {out!r}"
    )
