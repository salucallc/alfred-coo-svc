"""SAL-2905 — per-persona GitHub identity routing tests.

Covers ``persona_github.token_for_persona`` and the per-call-site
routing inside ``tools.py``. The load-bearing assertion is
``test_pr_review_uses_qa_token`` — that's the test that proves
hawkman won't trip the self-authored 422 in a split-token deployment.

Network is fully mocked. ``tools.os.environ`` reads happen via the
module-level imports of ``persona_github``, so ``monkeypatch.setenv``
is sufficient — no fragile ``importlib.reload`` dances.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from alfred_coo.persona import BUILTIN_PERSONAS
from alfred_coo.persona_github import (
    GitHubIdentityClass,
    PERSONA_IDENTITY_MAP,
    identity_class_for_persona,
    token_for_persona,
    set_current_persona,
    reset_current_persona,
)


# ── token_for_persona unit tests ────────────────────────────────────────────


def _clear_all_github_env(monkeypatch):
    for var in (
        "GITHUB_TOKEN",
        "GITHUB_TOKEN_BUILDER",
        "GITHUB_TOKEN_QA",
        "GITHUB_TOKEN_ORCHESTRATOR",
        "GITHUB_LOGIN_BUILDER",
        "GITHUB_LOGIN_QA",
        "GITHUB_LOGIN_ORCHESTRATOR",
    ):
        monkeypatch.delenv(var, raising=False)


def test_token_for_persona_builder_returns_builder_token(monkeypatch):
    _clear_all_github_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN_BUILDER", "builder-tok")
    monkeypatch.setenv("GITHUB_TOKEN", "legacy-tok")
    token, cls = token_for_persona("alfred-coo-a")
    assert token == "builder-tok"
    assert cls == GitHubIdentityClass.BUILDER


def test_token_for_persona_qa_returns_qa_token(monkeypatch):
    _clear_all_github_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN_QA", "qa-tok")
    monkeypatch.setenv("GITHUB_TOKEN", "legacy-tok")
    token, cls = token_for_persona("hawkman-qa-a")
    assert token == "qa-tok"
    assert cls == GitHubIdentityClass.QA


def test_token_for_persona_falls_back_to_legacy_when_per_identity_unset(monkeypatch):
    """Backwards compat: only GITHUB_TOKEN set → every persona resolves to legacy."""
    _clear_all_github_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "legacy-tok")
    for persona in ("alfred-coo-a", "hawkman-qa-a", "autonomous-build-a"):
        token, cls = token_for_persona(persona)
        assert token == "legacy-tok", f"{persona} did not fall back to legacy"
        assert cls == GitHubIdentityClass.UNKNOWN


def test_token_for_persona_unknown_persona_falls_back_to_legacy(monkeypatch):
    _clear_all_github_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "legacy-tok")
    token, cls = token_for_persona("not-a-real-persona")
    assert token == "legacy-tok"
    assert cls == GitHubIdentityClass.UNKNOWN


def test_token_for_persona_no_tokens_returns_empty(monkeypatch):
    """Caller's missing-token error path must still fire."""
    _clear_all_github_env(monkeypatch)
    token, cls = token_for_persona("alfred-coo-a")
    assert token == ""
    assert cls == GitHubIdentityClass.UNKNOWN


def test_token_for_persona_orchestrator_falls_back_to_qa_then_legacy(monkeypatch):
    """ORCHESTRATOR has a special chain: own → QA → legacy."""
    _clear_all_github_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN_QA", "qa-tok")
    monkeypatch.setenv("GITHUB_TOKEN", "legacy-tok")
    token, cls = token_for_persona("autonomous-build-a")
    assert token == "qa-tok"
    assert cls == GitHubIdentityClass.QA  # resolved via fallback

    # Now add the dedicated orchestrator token — should win.
    monkeypatch.setenv("GITHUB_TOKEN_ORCHESTRATOR", "orch-tok")
    token, cls = token_for_persona("autonomous-build-a")
    assert token == "orch-tok"
    assert cls == GitHubIdentityClass.ORCHESTRATOR

    # Now drop QA — orchestrator still resolves directly.
    monkeypatch.delenv("GITHUB_TOKEN_QA", raising=False)
    token, cls = token_for_persona("autonomous-build-a")
    assert token == "orch-tok"
    assert cls == GitHubIdentityClass.ORCHESTRATOR

    # Drop everything except legacy — orchestrator falls all the way through.
    monkeypatch.delenv("GITHUB_TOKEN_ORCHESTRATOR", raising=False)
    token, cls = token_for_persona("autonomous-build-a")
    assert token == "legacy-tok"
    assert cls == GitHubIdentityClass.UNKNOWN


def test_token_for_persona_strips_whitespace(monkeypatch):
    """Operators sometimes paste tokens with trailing newlines from .env files."""
    _clear_all_github_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN_BUILDER", "  builder-tok\n")
    token, cls = token_for_persona("alfred-coo-a")
    assert token == "builder-tok"


def test_identity_class_for_persona_unknown_returns_unknown_class():
    assert (
        identity_class_for_persona("not-mapped")
        == GitHubIdentityClass.UNKNOWN
    )
    assert identity_class_for_persona(None) == GitHubIdentityClass.UNKNOWN
    assert identity_class_for_persona("") == GitHubIdentityClass.UNKNOWN


def test_persona_identity_map_covers_all_pr_personas():
    """Meta-test: every persona declaring a GitHub-touching tool must
    appear in PERSONA_IDENTITY_MAP. Catches future personas added to
    persona.py that forget to register their identity class.

    A persona missing from the map will silently fall back to legacy
    GITHUB_TOKEN, which defeats split-identity for that persona's
    PRs / reviews. This test is the early-warning.
    """
    gh_tools = {"propose_pr", "update_pr", "pr_review", "pr_files_get"}
    missing: list[str] = []
    for name, persona in BUILTIN_PERSONAS.items():
        persona_tools = set(persona.tools or [])
        if persona_tools & gh_tools and name not in PERSONA_IDENTITY_MAP:
            missing.append(name)
    assert not missing, (
        f"personas with GitHub tools but no identity mapping: {missing} "
        "— add to PERSONA_IDENTITY_MAP in persona_github.py"
    )


# ── tools.py call-site routing ──────────────────────────────────────────────


class _FakeResponse:
    """Minimal urlopen() context-manager response for capturing the
    Authorization header without hitting network."""

    def __init__(self, payload: dict[str, Any]):
        self._payload = json.dumps(payload).encode()
        self.status = 200

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def captured_request():
    """Capture the urllib.request.Request passed to urlopen so we can
    assert which Authorization header was attached."""
    captured: dict[str, Any] = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["method"] = req.get_method()
        captured["data"] = req.data
        # Default success body — caller patches per-test if needed.
        return _FakeResponse(
            {"id": 999, "html_url": "https://example/x", "state": "APPROVED",
             "submitted_at": "2026-04-25T00:00:00Z", "ok": True, "merged": True,
             "sha": "abc123"}
        )

    with patch("alfred_coo.tools.urllib.request.urlopen", side_effect=_fake_urlopen):
        yield captured


@pytest.mark.asyncio
async def test_pr_review_uses_qa_token(monkeypatch, captured_request):
    """LOAD-BEARING: pr_review must use GITHUB_TOKEN_QA when set, even
    if the active persona context is a builder. The tool's own
    intended-class is QA — that's what's authoritative for the
    review POST."""
    _clear_all_github_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN_BUILDER", "builder-tok")
    monkeypatch.setenv("GITHUB_TOKEN_QA", "qa-tok")
    monkeypatch.setenv("GITHUB_TOKEN", "legacy-tok")

    from alfred_coo.tools import pr_review

    result = await pr_review(
        owner="salucallc",
        repo="alfred-coo-svc",
        pr_number=1,
        event="APPROVE",
        body="lgtm",
    )
    assert "error" not in result, result
    auth_header = captured_request["headers"].get(
        "Authorization", captured_request["headers"].get("authorization", "")
    )
    assert auth_header == "Bearer qa-tok", (
        f"pr_review used wrong token; got {auth_header!r}, expected Bearer qa-tok"
    )


@pytest.mark.asyncio
async def test_pr_review_falls_back_to_legacy_token_when_qa_unset(
    monkeypatch, captured_request
):
    """Single-token deployment: only GITHUB_TOKEN set. pr_review still
    works (legacy fallback) — preserves today's behaviour incl. the
    self-authored 422 fallback."""
    _clear_all_github_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "legacy-tok")

    from alfred_coo.tools import pr_review

    result = await pr_review(
        owner="salucallc",
        repo="alfred-coo-svc",
        pr_number=1,
        event="COMMENT",
        body="hi",
    )
    assert "error" not in result, result
    auth_header = captured_request["headers"].get(
        "Authorization", captured_request["headers"].get("authorization", "")
    )
    assert auth_header == "Bearer legacy-tok"


@pytest.mark.asyncio
async def test_pr_review_no_token_at_all_returns_error(monkeypatch):
    """No tokens → existing missing-token error path fires unchanged."""
    _clear_all_github_env(monkeypatch)
    from alfred_coo.tools import pr_review

    result = await pr_review(
        owner="salucallc",
        repo="alfred-coo-svc",
        pr_number=1,
        event="COMMENT",
        body="hi",
    )
    assert "error" in result
    assert "GITHUB_TOKEN" in result["error"]


@pytest.mark.asyncio
async def test_github_merge_pr_uses_orchestrator_token(monkeypatch, captured_request):
    """github_merge_pr is orchestrator-class. With dedicated token set,
    that wins over QA and legacy."""
    _clear_all_github_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN_ORCHESTRATOR", "orch-tok")
    monkeypatch.setenv("GITHUB_TOKEN_QA", "qa-tok")
    monkeypatch.setenv("GITHUB_TOKEN", "legacy-tok")

    from alfred_coo.tools import github_merge_pr

    result = await github_merge_pr(
        owner="salucallc",
        repo="alfred-coo-svc",
        pr_number=1,
    )
    assert "error" not in result, result
    auth = captured_request["headers"].get(
        "Authorization", captured_request["headers"].get("authorization", "")
    )
    assert auth == "Bearer orch-tok", auth


@pytest.mark.asyncio
async def test_github_merge_pr_falls_back_to_qa_token(monkeypatch, captured_request):
    """No GITHUB_TOKEN_ORCHESTRATOR but GITHUB_TOKEN_QA set →
    orchestrator chain falls back to QA. Documented in §4.4 of the
    design doc — 'QA approved → QA merges' when no dedicated
    orchestrator bot exists."""
    _clear_all_github_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN_QA", "qa-tok")
    monkeypatch.setenv("GITHUB_TOKEN", "legacy-tok")

    from alfred_coo.tools import github_merge_pr

    result = await github_merge_pr(
        owner="salucallc",
        repo="alfred-coo-svc",
        pr_number=1,
    )
    assert "error" not in result, result
    auth = captured_request["headers"].get(
        "Authorization", captured_request["headers"].get("authorization", "")
    )
    assert auth == "Bearer qa-tok", auth


@pytest.mark.asyncio
async def test_pr_files_get_uses_qa_token(monkeypatch):
    """pr_files_get is QA-class (read-only, but routes through the
    QA identity for audit-trail cohesion)."""
    _clear_all_github_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN_QA", "qa-tok")
    monkeypatch.setenv("GITHUB_TOKEN", "legacy-tok")

    captured_tokens: list[str] = []

    async def _fake_get_json(url: str, token: str):
        captured_tokens.append(token)
        if "/pulls/" in url and "/files" not in url:
            return ({"head": {"sha": "abc", "ref": "feat"}, "base": {"ref": "main"}}, None)
        if "/files" in url:
            return ([], None)
        return ({}, None)

    from alfred_coo import tools as tools_mod
    monkeypatch.setattr(tools_mod, "_github_api_get_json", _fake_get_json)

    result = await tools_mod.pr_files_get(
        owner="salucallc",
        repo="alfred-coo-svc",
        pr_number=1,
    )
    assert "error" not in result, result
    assert captured_tokens, "pr_files_get did not call _github_api_get_json"
    assert all(
        t == "qa-tok" for t in captured_tokens
    ), f"pr_files_get used wrong token(s): {captured_tokens}"


@pytest.mark.asyncio
async def test_persona_context_does_not_override_tool_intended_class(
    monkeypatch, captured_request
):
    """Defence in depth: even with a builder-persona ContextVar
    active, pr_review (intended-class=QA) routes through the QA
    token. The tool's intended class is authoritative — that's the
    whole point of split-identity. If a builder persona's tools list
    were ever (mis)configured to include pr_review, the QA token
    still drives the call so hawkman's 422 trap doesn't fire.
    """
    _clear_all_github_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN_BUILDER", "builder-tok")
    monkeypatch.setenv("GITHUB_TOKEN_QA", "qa-tok")
    monkeypatch.setenv("GITHUB_TOKEN", "legacy-tok")

    from alfred_coo.tools import pr_review

    persona_token = set_current_persona("alfred-coo-a")  # builder context
    try:
        result = await pr_review(
            owner="salucallc",
            repo="alfred-coo-svc",
            pr_number=1,
            event="APPROVE",
            body="x",
        )
    finally:
        reset_current_persona(persona_token)

    assert "error" not in result, result
    auth = captured_request["headers"].get(
        "Authorization", captured_request["headers"].get("authorization", "")
    )
    # Intended class (QA) wins regardless of persona ContextVar.
    assert auth == "Bearer qa-tok", auth
