"""Regression tests for ``SoulClient.recent_memories`` against the real
soul-svc v2.0.0 endpoint surface.

Background — why this file exists
================================

PR #150 shipped Fix A (wave-skip cache): on wave entry, the orchestrator
calls ``state.lookup_wave_pass`` → ``soul_client.recent_memories(topics=[
"autonomous_build:wave_pass:<project>:wave_<n>"])`` and skips the wave
when a fresh ratio=1.00 record is found. PR #150's tests passed (they
mocked ``recent_memories`` directly). Production never fired the skip.

Diagnosis (2026-04-27):
  * Wave-pass records ARE being persisted: real soul-svc has them under
    ``GET /v1/memory/alfred-coo`` with topic
    ``autonomous_build:wave_pass:8c1d8f69-...:wave_0`` and ratio=1.0.
  * The read path was broken in two ways simultaneously:
      1. ``SoulClient.recent_memories`` called ``GET /v1/memory/recent``,
         which does NOT exist on soul-svc v2.0.0. The real router maps
         the request to ``GET /v1/memory/{session_id}`` with
         ``session_id="recent"`` and returns an empty list. Every
         ``lookup_wave_pass`` therefore got ``[]`` and ``_should_skip_wave``
         always returned False.
      2. soul-svc returns each memory's payload under ``full_context``
         (plus ``topic_id``); callers and ``lookup_wave_pass`` read
         ``mem.get("content")``. Even if the right endpoint had been hit,
         the records would have been silently dropped.

The fix is in ``alfred_coo.soul``: ``recent_memories`` now calls the real
``GET /v1/memory/{session_id}`` endpoint, fetches a generous window when a
``topics`` filter is active, and normalizes ``full_context`` →
``content`` plus ``topic_id`` → ``topics`` so the existing callers still
match on ``content`` / ``topics`` keys.

These tests run a stub soul-svc via ``httpx.AsyncBaseTransport`` and would
fail against the pre-fix client, so a future regression of either bug
will be caught at CI time.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from alfred_coo.soul import SoulClient


# ── Stub soul-svc ───────────────────────────────────────────────────────


class _StubSoulSvc(httpx.AsyncBaseTransport):
    """Mimics soul-svc v2.0.0's actually-existing endpoints.

    GET /v1/memory/{session_id}?limit=N
        Returns ``{"memories": [...], "count": N, "session_id": ...}``,
        with each memory carrying ``id``, ``session_id``, ``topic_id``,
        ``full_context``, ``topics``, ``created_at`` (no ``content`` key).

    GET /v1/memory/recent?...
        Does NOT exist. The real soul-svc routes this to
        ``/v1/memory/{session_id="recent"}`` and returns an empty list.
        We model that exact behaviour so a regressed client (one that
        re-introduces the bad URL) is caught.

    POST /v1/memory/write
        Records the body and returns a fake memory_id.
    """

    def __init__(self, memories: list[dict] | None = None):
        self.requests: list[httpx.Request] = []
        # Newest-first ordering matches real soul-svc semantics.
        self.memories: list[dict] = list(memories or [])
        self.writes: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        parsed = urlparse(str(request.url))
        path = parsed.path
        method = request.method
        if method == "GET" and path.startswith("/v1/memory/"):
            session_id = path[len("/v1/memory/") :]
            params = parse_qs(parsed.query)
            try:
                limit = int(params.get("limit", ["20"])[0])
            except (TypeError, ValueError):
                limit = 20
            # The real soul-svc treats `recent` as a literal session_id:
            # there is no entry with that session, so the result is empty.
            if session_id == "recent":
                body = {"memories": [], "count": 0, "session_id": "recent"}
            else:
                matches = [
                    m for m in self.memories if m.get("session_id") == session_id
                ]
                body = {
                    "memories": matches[:limit],
                    "count": len(matches[:limit]),
                    "session_id": session_id,
                }
            return httpx.Response(200, json=body, request=request)
        if method == "POST" and path == "/v1/memory/write":
            try:
                payload: Any = httpx._content.json_decode(request.content)  # type: ignore[attr-defined]
            except Exception:
                import json as _json
                payload = _json.loads(request.content.decode("utf-8") or "{}")
            self.writes.append(payload)
            return httpx.Response(
                200,
                json={
                    "memory_id": f"mem-{len(self.writes)}",
                    "session_id": payload.get("session_id"),
                    "content_hash": "deadbeef",
                    "topics": payload.get("topics") or [],
                    "created_at": "2026-04-27T07:00:00Z",
                },
                request=request,
            )
        return httpx.Response(404, json={"detail": "not found"}, request=request)


def _mk_memory(
    *,
    session_id: str,
    topic_id: str,
    full_context: str,
    created_at: str,
    extra_topics: list[str] | None = None,
    mem_id: str = "m1",
) -> dict:
    """Build a soul-svc-shaped memory record (matches the real /v1/memory/{session_id}
    response we observed in production on 2026-04-27).
    """
    topics = [topic_id]
    if extra_topics:
        topics.extend(extra_topics)
    return {
        "id": mem_id,
        "session_id": session_id,
        "topic_id": topic_id,
        "full_context": full_context,
        "full_context_hash": "0c1b9d0a",
        "summarized_context": "",
        "topics": topics,
        "metadata": {"node_type": "full"},
        "created_at": created_at,
    }


def _mk_client(transport: _StubSoulSvc, *, session_id: str = "alfred-coo") -> SoulClient:
    client = SoulClient(
        base_url="http://soul.test",
        api_key="test-key",
        session_id=session_id,
    )
    # Replace the internal httpx client so our transport intercepts every call.
    client._client = httpx.AsyncClient(transport=transport)
    return client


# ── The actual regression: PR #150 wave-skip read path ──────────────────


@pytest.mark.asyncio
async def test_recent_memories_finds_wave_pass_record_against_real_endpoint():
    """End-to-end regression for the PR #150 wave-skip read path.

    Mirrors the production scenario observed on 2026-04-27:
      * A wave_pass record exists for project 8c1d8f69... wave_0 with
        ratio=1.0 and the correct topic.
      * The orchestrator calls ``recent_memories(topics=[topic])`` via
        ``lookup_wave_pass``.
      * BEFORE the fix: ``recent_memories`` GET'd ``/v1/memory/recent``
        (a non-existent path that soul-svc routes to session_id="recent",
        always empty). Result: ``[]`` — wave-skip never fires.
      * AFTER the fix: ``recent_memories`` GET's
        ``/v1/memory/{session_id}`` (real endpoint), filters by topic,
        and normalizes ``full_context`` → ``content`` so the orchestrator's
        ``mem.get("content")`` finds the JSON payload.
    """
    project_id = "8c1d8f69-359d-457a-a11c-2e650863774c"
    wave_n = 0
    topic = f"autonomous_build:wave_pass:{project_id}:wave_{wave_n}"
    payload = (
        '{"denominator": 5, "green_count": 5, '
        f'"linear_project_id": "{project_id}", '
        '"passed_at": "2026-04-27T06:01:24Z", "ratio": 1.0, '
        '"ticket_codes_seen": ["SAL-2589"], "wave_n": 0}'
    )
    transport = _StubSoulSvc(
        memories=[
            _mk_memory(
                session_id="alfred-coo",
                topic_id=topic,
                full_context=payload,
                created_at="2026-04-27T06:01:24.850205+00:00",
                extra_topics=["autonomous_build", "wave_pass"],
                mem_id="real-wave-pass",
            ),
        ],
    )
    client = _mk_client(transport)

    result = await client.recent_memories(limit=5, topics=[topic])

    # The fix: we got the record back. Pre-fix this list was empty.
    assert isinstance(result, list)
    assert len(result) == 1, f"expected 1 wave_pass match, got {result!r}"
    rec = result[0]
    # Normalisation: callers (lookup_wave_pass) read .content. Real soul-svc
    # returns full_context. The client must map that.
    assert rec.get("content") == payload
    assert topic in (rec.get("topics") or [])

    # Verify the URL we hit is the REAL endpoint, not the broken /recent one.
    paths = [urlparse(str(r.url)).path for r in transport.requests]
    assert "/v1/memory/alfred-coo" in paths, (
        f"recent_memories must hit /v1/memory/{{session_id}} — got {paths!r}"
    )
    assert not any(p.endswith("/v1/memory/recent") for p in paths), (
        f"recent_memories must NOT hit the non-existent /v1/memory/recent — got {paths!r}"
    )

    await client.close()


@pytest.mark.asyncio
async def test_recent_memories_filters_by_topic_among_many():
    """Topic filter must select only matching entries from a noisy session."""
    transport = _StubSoulSvc(
        memories=[
            _mk_memory(
                session_id="alfred-coo",
                topic_id="autonomous_build:other-kickoff:state",
                full_context="{}",
                created_at="2026-04-27T06:00:01Z",
                mem_id="m-state",
            ),
            _mk_memory(
                session_id="alfred-coo",
                topic_id="autonomous_build:wave_pass:proj-a:wave_0",
                full_context='{"ratio": 1.0, "wave_n": 0}',
                created_at="2026-04-27T06:00:02Z",
                mem_id="m-wave-a",
            ),
            _mk_memory(
                session_id="alfred-coo",
                topic_id="autonomous_build:wave_pass:proj-b:wave_0",
                full_context='{"ratio": 1.0, "wave_n": 0}',
                created_at="2026-04-27T06:00:03Z",
                mem_id="m-wave-b",
            ),
        ],
    )
    client = _mk_client(transport)
    result = await client.recent_memories(
        limit=5,
        topics=["autonomous_build:wave_pass:proj-a:wave_0"],
    )
    assert len(result) == 1
    assert result[0]["id"] == "m-wave-a"
    await client.close()


@pytest.mark.asyncio
async def test_recent_memories_topic_filter_widens_fetch():
    """When a topic filter is active, the client must fetch a generous
    window (>= 200) so the filter has enough material; otherwise a busy
    session can hide the matching entry past the requested ``limit``.
    """
    transport = _StubSoulSvc(
        memories=[
            _mk_memory(
                session_id="alfred-coo",
                topic_id="something_else",
                full_context="{}",
                created_at="2026-04-27T06:00:00Z",
                mem_id=f"noise-{i}",
            )
            for i in range(20)
        ],
    )
    client = _mk_client(transport)
    await client.recent_memories(limit=5, topics=["narrow_topic"])
    # Inspect the GET that actually went out: limit query param should be
    # at least 200 (the topic-filter floor), NOT 5.
    assert len(transport.requests) == 1
    qs = parse_qs(urlparse(str(transport.requests[0].url)).query)
    assert int(qs["limit"][0]) >= 200, (
        f"topic-filter fetch must use the wider floor; saw limit={qs.get('limit')!r}"
    )
    await client.close()


@pytest.mark.asyncio
async def test_recent_memories_unfiltered_passes_limit_through():
    transport = _StubSoulSvc(
        memories=[
            _mk_memory(
                session_id="alfred-coo",
                topic_id=f"t-{i}",
                full_context="{}",
                created_at=f"2026-04-27T06:00:{i:02d}Z",
                mem_id=f"m-{i}",
            )
            for i in range(5)
        ],
    )
    client = _mk_client(transport)
    result = await client.recent_memories(limit=3)
    assert len(result) == 3
    qs = parse_qs(urlparse(str(transport.requests[0].url)).query)
    # No topic filter → use the caller's limit verbatim.
    assert int(qs["limit"][0]) == 3
    await client.close()


@pytest.mark.asyncio
async def test_recent_memories_empty_session_returns_empty_list():
    transport = _StubSoulSvc(memories=[])
    client = _mk_client(transport)
    result = await client.recent_memories(limit=5, topics=["anything"])
    assert result == []
    await client.close()


@pytest.mark.asyncio
async def test_recent_memories_normalizes_content_and_topics():
    """Belt-and-braces: the normalization must surface ``content`` even
    when soul-svc only returned ``full_context`` and an empty ``topics``
    list (only ``topic_id``). This is what we observed in production.
    """
    transport = _StubSoulSvc(
        memories=[
            {
                "id": "minimal",
                "session_id": "alfred-coo",
                "topic_id": "autonomous_build:wave_pass:proj:wave_3",
                "full_context": '{"ratio": 1.0}',
                # Notice: no `content` key, no `topics` array
                "created_at": "2026-04-27T06:00:00Z",
            },
        ],
    )
    client = _mk_client(transport)
    result = await client.recent_memories(
        limit=5,
        topics=["autonomous_build:wave_pass:proj:wave_3"],
    )
    assert len(result) == 1
    rec = result[0]
    assert rec["content"] == '{"ratio": 1.0}'
    assert "autonomous_build:wave_pass:proj:wave_3" in rec["topics"]
    await client.close()
