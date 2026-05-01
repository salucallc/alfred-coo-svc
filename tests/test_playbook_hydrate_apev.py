"""Tests for the hydrate_apev_headings playbook.

Covers:
* Idempotency — tickets already carrying the canonical heading are skipped
* Inline ``APE/V:`` extraction — handles common variants and stops at boundaries
* Done/Cancelled state filter — terminal tickets are not rewritten
* Bounded action — at most ``max_actions_per_tick`` tickets are mutated per tick
* Dry-run — no mutation is attempted, just a "would hydrate" notable line
* Wet-run — issueUpdate mutations fire and notable lines reflect successes
* Error path — scan failure for one project doesn't poison the rest
* Missing key — playbook returns an error result, doesn't raise
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from alfred_coo.autonomous_build.playbooks import (
    DEFAULT_PLAYBOOKS,
    HydrateAPEVHeadingsPlaybook,
    PlaybookResult,
)
from alfred_coo.autonomous_build.playbooks.hydrate_apev import (
    CANONICAL_HEADING,
    _extract_apev_text,
    _render_canonical_section,
)


# ── Pure helpers ────────────────────────────────────────────────────────────


def test_extract_apev_text_basic():
    body = "Some intro.\n\nAPE/V: every endpoint returns 200\n\nMore prose."
    assert _extract_apev_text(body) == "every endpoint returns 200"


def test_extract_apev_text_stops_at_heading():
    body = "APE/V: rule one\nrule two\n## Next Section\nirrelevant"
    out = _extract_apev_text(body)
    assert out is not None
    assert "rule one" in out
    assert "Next Section" not in out


def test_extract_apev_text_handles_variants():
    assert _extract_apev_text("APEV: foo bar") == "foo bar"
    assert _extract_apev_text("Acceptance (APE/V): qux") == "qux"


def test_extract_apev_text_returns_none_when_absent():
    assert _extract_apev_text("nothing here") is None


def test_render_canonical_section_byte_stable():
    text = "every endpoint returns 200"
    a = _render_canonical_section(text)
    b = _render_canonical_section(text)
    assert a == b
    assert CANONICAL_HEADING in a
    assert text in a


# ── Default registry ────────────────────────────────────────────────────────


def test_default_registry_contains_hydrate():
    """The default playbook list must include the hydrate playbook (lifted
    from the one-off ``Z:/_tmp/file_hydrate_apev_headings.py`` script)."""
    kinds = [p.kind for p in DEFAULT_PLAYBOOKS]
    assert "hydrate_apev_headings" in kinds


# ── Fake httpx client ───────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, body: dict[str, Any]):
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeAsyncClient:
    """Minimal asynccontextmanager-shaped fake of httpx.AsyncClient."""

    def __init__(self, project_responses: dict[str, dict], mutation_handler=None):
        # project_responses keyed by project_id → response dict
        self.project_responses = project_responses
        self.mutation_handler = mutation_handler
        self.posts: list[dict[str, Any]] = []
        # Track how many entered/exited
        self._open = False

    async def __aenter__(self):
        self._open = True
        return self

    async def __aexit__(self, *exc):
        self._open = False
        return False

    async def post(self, url: str, *, headers, content):
        body = json.loads(content.decode())
        q = body.get("query", "")
        self.posts.append({"url": url, "query": q, "vars": body.get("variables")})
        if "issueUpdate" in q:
            if self.mutation_handler is not None:
                return self.mutation_handler(body["variables"])
            return _FakeResponse({"data": {"issueUpdate": {"success": True}}})
        # project query
        pid = (body.get("variables") or {}).get("pid")
        return _FakeResponse(self.project_responses.get(pid, {"data": {"project": {"issues": {"nodes": []}}}}))


def _patch_client(monkeypatch, fake: _FakeAsyncClient):
    """Replace ``httpx.AsyncClient`` inside the playbook module with a
    factory returning the supplied fake."""
    def factory(*args, **kwargs):
        return fake
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.playbooks.hydrate_apev.httpx.AsyncClient",
        factory,
    )


def _project_payload(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"data": {"project": {"issues": {"nodes": nodes}}}}


# ── Idempotency + filters ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skips_tickets_already_carrying_canonical_heading(monkeypatch):
    pid = "p-1"
    pb = HydrateAPEVHeadingsPlaybook(projects={"only": pid})
    fake = _FakeAsyncClient(
        project_responses={pid: _project_payload([
            {
                "id": "uuid-1",
                "identifier": "SAL-1001",
                "title": "Already hydrated",
                "description": (
                    "APE/V: foo\n\n"
                    + CANONICAL_HEADING
                    + "\n\nfoo"
                ),
                "state": {"name": "Backlog"},
            },
        ])},
    )
    _patch_client(monkeypatch, fake)
    res = await pb.execute(linear_api_key="key", dry_run=True)
    assert res.candidates_found == 0
    assert res.actions_taken == 0


@pytest.mark.asyncio
async def test_skips_done_and_cancelled_states(monkeypatch):
    """Terminal-state tickets must be left alone — rewriting Done bodies
    is noise and risks resurfacing closed work in dashboards."""
    pid = "p-1"
    pb = HydrateAPEVHeadingsPlaybook(projects={"only": pid})
    fake = _FakeAsyncClient(
        project_responses={pid: _project_payload([
            {
                "id": "u-done",
                "identifier": "SAL-1100",
                "title": "Done with inline apev",
                "description": "APE/V: should not hydrate",
                "state": {"name": "Done"},
            },
            {
                "id": "u-cancel",
                "identifier": "SAL-1101",
                "title": "Cancelled with inline apev",
                "description": "APE/V: also no",
                "state": {"name": "Cancelled"},
            },
            {
                "id": "u-active",
                "identifier": "SAL-1102",
                "title": "Active should hydrate",
                "description": "APE/V: yes please",
                "state": {"name": "Backlog"},
            },
        ])},
    )
    _patch_client(monkeypatch, fake)
    res = await pb.execute(linear_api_key="key", dry_run=True)
    assert res.candidates_found == 1
    assert "SAL-1102" in " ".join(res.notable)


# ── Dry-run + wet-run ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_does_not_mutate(monkeypatch):
    pid = "p-1"
    pb = HydrateAPEVHeadingsPlaybook(projects={"only": pid})
    fake = _FakeAsyncClient(
        project_responses={pid: _project_payload([
            {
                "id": "u-1",
                "identifier": "SAL-2001",
                "title": "Needs hydration",
                "description": "APE/V: dry-run candidate",
                "state": {"name": "Backlog"},
            },
        ])},
    )
    _patch_client(monkeypatch, fake)
    res = await pb.execute(linear_api_key="key", dry_run=True)
    assert res.candidates_found == 1
    assert res.actions_taken == 0
    assert res.dry_run is True
    assert any("would hydrate SAL-2001" in n for n in res.notable)
    # Only the project query — no issueUpdate mutations were sent.
    assert all("issueUpdate" not in p["query"] for p in fake.posts)


@pytest.mark.asyncio
async def test_wet_run_mutates_and_records(monkeypatch):
    pid = "p-1"
    pb = HydrateAPEVHeadingsPlaybook(projects={"only": pid})
    fake = _FakeAsyncClient(
        project_responses={pid: _project_payload([
            {
                "id": "u-1",
                "identifier": "SAL-3001",
                "title": "Wet-run target",
                "description": "preamble\n\nAPE/V: ship it\n\ntrailing prose",
                "state": {"name": "Backlog"},
            },
        ])},
    )
    _patch_client(monkeypatch, fake)
    res = await pb.execute(linear_api_key="key", dry_run=False)
    assert res.candidates_found == 1
    assert res.actions_taken == 1
    assert res.dry_run is False
    assert any("hydrated SAL-3001" in n for n in res.notable)
    mutations = [p for p in fake.posts if "issueUpdate" in p["query"]]
    assert len(mutations) == 1
    new_body = mutations[0]["vars"]["body"]
    assert CANONICAL_HEADING in new_body
    assert "ship it" in new_body


# ── Bounded actions ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_caps_actions_per_tick(monkeypatch):
    """When more candidates exist than ``max_actions_per_tick``, only the
    cap is acted on this tick and the rest are reported as skipped."""
    pid = "p-1"
    pb = HydrateAPEVHeadingsPlaybook(projects={"only": pid})
    pb.max_actions_per_tick = 2
    nodes = [
        {
            "id": f"u-{i}",
            "identifier": f"SAL-40{i:02}",
            "title": f"t{i}",
            "description": f"APE/V: variant {i}",
            "state": {"name": "Backlog"},
        }
        for i in range(5)
    ]
    fake = _FakeAsyncClient(project_responses={pid: _project_payload(nodes)})
    _patch_client(monkeypatch, fake)
    res = await pb.execute(linear_api_key="key", dry_run=False)
    assert res.candidates_found == 5
    assert res.actions_taken == 2
    assert res.actions_skipped == 3


# ── Error paths ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_key_returns_error_result(monkeypatch):
    """Empty key → playbook returns a result with errors, never raises."""
    pb = HydrateAPEVHeadingsPlaybook(projects={"x": "p-1"})
    res = await pb.execute(linear_api_key="", dry_run=True)
    assert res.candidates_found == 0
    assert res.errors and "linear_api_key" in res.errors[0]


@pytest.mark.asyncio
async def test_one_project_scan_failure_doesnt_poison_others(monkeypatch):
    """A single project's scan raising should be recorded as an error
    while remaining projects are still scanned (best-effort)."""
    p_ok, p_bad = "p-ok", "p-bad"
    pb = HydrateAPEVHeadingsPlaybook(projects={"good": p_ok, "bad": p_bad})

    class _RaisingAsyncClient(_FakeAsyncClient):
        async def post(self, url, *, headers, content):
            body = json.loads(content.decode())
            pid = (body.get("variables") or {}).get("pid")
            if pid == p_bad:
                raise httpx.ConnectError("simulated network glitch")
            return await super().post(url, headers=headers, content=content)

    fake = _RaisingAsyncClient(
        project_responses={p_ok: _project_payload([
            {
                "id": "u-1",
                "identifier": "SAL-5001",
                "title": "good",
                "description": "APE/V: ok",
                "state": {"name": "Backlog"},
            },
        ])},
    )
    _patch_client(monkeypatch, fake)
    res = await pb.execute(linear_api_key="key", dry_run=True)
    assert res.candidates_found == 1
    assert any("bad" in e and "scan_failed" in e for e in res.errors)


# ── Result rendering ───────────────────────────────────────────────────────


def test_silent_result_renders_no_lines():
    pr = PlaybookResult(kind="hydrate_apev_headings", dry_run=True)
    assert pr.is_silent() is True
    assert pr.render_digest_lines() == []


def test_dry_result_with_finds_renders_dry_prefix():
    pr = PlaybookResult(
        kind="hydrate_apev_headings",
        candidates_found=3,
        dry_run=True,
        notable=["would hydrate SAL-1", "would hydrate SAL-2", "would hydrate SAL-3"],
    )
    lines = pr.render_digest_lines()
    assert any("[dry]" in line and "found=3" in line for line in lines)


def test_wet_result_renders_acted_count():
    pr = PlaybookResult(
        kind="hydrate_apev_headings",
        candidates_found=2,
        actions_taken=2,
        dry_run=False,
        notable=["hydrated SAL-1", "hydrated SAL-2"],
    )
    lines = pr.render_digest_lines()
    assert any("acted=2" in line for line in lines)
    assert all("[dry]" not in line for line in lines)
