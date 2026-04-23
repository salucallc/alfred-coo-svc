"""AB-06 tests: SS-08 gate (JWS schema post + Slack ACK polling).

Covers both the standalone `run_ss08_gate` driver and the
orchestrator `_maybe_ss08_gate` wiring. Production Slack + mesh
clients are always stubbed; `asyncio.sleep` is monkeypatched to a
no-op + an advancing wall clock so timeout paths complete in
milliseconds.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from alfred_coo.autonomous_build import ss08_gate as gate_mod
from alfred_coo.autonomous_build.graph import (
    Ticket,
    TicketStatus,
)
from alfred_coo.autonomous_build.orchestrator import (
    AutonomousBuildOrchestrator,
)
from alfred_coo.autonomous_build.ss08_gate import (
    ACK_KEYWORDS,
    CRISTIAN_SLACK_USER_ID,
    run_ss08_gate,
)


# ── Fakes ──────────────────────────────────────────────────────────────────


class _SpyCadence:
    """Minimal cadence stand-in: captures every `post()` call + exposes a
    `channel` attribute matching the real `SlackCadence`."""

    def __init__(self, channel: str = "C0ASAKFTR1C") -> None:
        self.channel = channel
        self.posts: List[str] = []

    async def post(self, message: str) -> Dict[str, Any]:
        self.posts.append(message)
        return {"ok": True, "ts": f"{len(self.posts)}.0"}


class _FakeMesh:
    async def create_task(self, *, title, description="", from_session_id=None):
        return {"id": "child-1", "title": title, "status": "pending"}

    async def list_tasks(self, status=None, limit=50):
        return []

    async def complete(self, task_id, *, session_id, status=None, result=None):
        return None


class _FakeSoul:
    def __init__(self) -> None:
        self.writes: List[Dict[str, Any]] = []

    async def write_memory(self, content, topics=None):
        self.writes.append({"content": content, "topics": topics or []})
        return {"memory_id": f"m-{len(self.writes)}"}

    async def recent_memories(self, limit=5, topics=None):
        return []


class _FakeSettings:
    soul_session_id = "test-session"
    soul_node_id = "test-node"
    soul_harness = "pytest"


def _mk_orchestrator(
    mesh: Optional[Any] = None,
    soul: Optional[Any] = None,
) -> AutonomousBuildOrchestrator:
    task = {
        "id": "kick-ab06",
        "title": "[persona:autonomous-build-a] kickoff",
        "description": "",
    }

    class P:
        name = "autonomous-build-a"
        handler = "AutonomousBuildOrchestrator"

    return AutonomousBuildOrchestrator(
        task=task,
        persona=P(),
        mesh=mesh or _FakeMesh(),
        soul=soul or _FakeSoul(),
        dispatcher=object(),
        settings=_FakeSettings(),
    )


def _mk_ss08_ticket(is_critical_path: bool = True) -> Ticket:
    return Ticket(
        id="u-ss08",
        identifier="SAL-9999",
        code="SS-08",
        title="SAL-9999 SS-08 PQ receipt endpoint",
        wave=1,
        epic="tiresias",
        size="M",
        estimate=5,
        is_critical_path=is_critical_path,
    )


@pytest.fixture
def _no_sleep_time(monkeypatch):
    """Replace `ss08_gate.asyncio.sleep` with a no-op that advances a
    virtual clock used by `ss08_gate.time.time`. Yields the clock dict
    so tests can inspect/advance it directly.
    """
    clock = {"now": 10_000.0}

    def _time() -> float:
        return clock["now"]

    async def _sleep(delay) -> None:
        try:
            clock["now"] += float(delay or 0)
        except (TypeError, ValueError):
            pass

    monkeypatch.setattr(gate_mod.time, "time", _time)
    monkeypatch.setattr(gate_mod.asyncio, "sleep", _sleep)
    return clock


# ── run_ss08_gate ─────────────────────────────────────────────────────────


async def test_ack_detected_proceeds(_no_sleep_time):
    """First poll returns matched=True → gate returns True + two cadence posts."""
    cadence = _SpyCadence()
    poll_calls: List[Dict[str, Any]] = []

    async def fake_poll(**kwargs):
        poll_calls.append(kwargs)
        return {
            "matched": True,
            "message_ts": "1234.567",
            "text": "ACK SS-08",
            "matched_keyword": "ack\\s*ss[-_\\s]?08",
        }

    result = await run_ss08_gate(cadence=cadence, slack_ack_poll_fn=fake_poll)

    assert result is True
    # Two posts: schema + ack confirmation.
    assert len(cadence.posts) == 2
    assert "JWS claims schema" in cadence.posts[0]
    assert "tenant_id" in cadence.posts[0]
    assert "✅" in cadence.posts[1] or "acknowledged" in cadence.posts[1]
    # Poll called with the channel + hardcoded user id + ACK keywords.
    assert len(poll_calls) == 1
    assert poll_calls[0]["channel"] == cadence.channel
    assert poll_calls[0]["author_user_id"] == CRISTIAN_SLACK_USER_ID
    assert poll_calls[0]["keywords"] == ACK_KEYWORDS


async def test_ack_detected_after_multiple_polls(_no_sleep_time):
    """First 2 polls miss, 3rd matches → returns True; ~3 poll calls."""
    cadence = _SpyCadence()
    calls = {"n": 0}

    async def fake_poll(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            return {"matched": False}
        return {
            "matched": True,
            "message_ts": "1234.999",
            "matched_keyword": "approve ss-08",
        }

    result = await run_ss08_gate(cadence=cadence, slack_ack_poll_fn=fake_poll)

    assert result is True
    assert calls["n"] == 3
    # Schema + ack confirmation posts.
    assert len(cadence.posts) == 2


async def test_timeout_after_4h_returns_false(_no_sleep_time):
    """No ACK ever arrives; wall-clock crosses GATE_TIMEOUT_SECONDS → False."""
    cadence = _SpyCadence()
    calls = {"n": 0}

    async def fake_poll(**kwargs):
        calls["n"] += 1
        return {"matched": False}

    # Sleep step is GATE_POLL_INTERVAL_SECONDS; the fake sleep advances
    # the virtual clock so the 4h timeout hits after ceil(4h / 2min) = 120
    # iterations. That's fast under the fake clock.
    result = await run_ss08_gate(cadence=cadence, slack_ack_poll_fn=fake_poll)

    assert result is False
    # First post = schema; last post should be the timeout defer message.
    assert len(cadence.posts) >= 2
    assert "JWS claims schema" in cadence.posts[0]
    timeout_msg = cadence.posts[-1]
    assert "timed out" in timeout_msg.lower()
    assert "v1.1" in timeout_msg
    # Should have polled many times before timing out.
    assert calls["n"] > 1


async def test_transient_network_error_retries(_no_sleep_time):
    """First poll raises, second matches → gate still returns True."""
    cadence = _SpyCadence()
    call_log: List[str] = []

    async def fake_poll(**kwargs):
        call_log.append("call")
        if len(call_log) == 1:
            raise ConnectionError("simulated network blip")
        if len(call_log) == 2:
            # Also exercise the error-key transient branch.
            return {"error": "slack 503"}
        return {"matched": True, "matched_keyword": "ack ss-08"}

    result = await run_ss08_gate(cadence=cadence, slack_ack_poll_fn=fake_poll)

    assert result is True
    assert len(call_log) == 3
    # Gate should still have issued the ack confirmation after retry.
    assert any("acknowledged" in p for p in cadence.posts)


async def test_schema_post_failure_returns_false(_no_sleep_time):
    """If we can't even post the schema, gate aborts with False."""

    class _BadCadence:
        channel = "C0ASAKFTR1C"

        async def post(self, message):
            raise RuntimeError("slack down")

    async def _never_polled(**_kwargs):
        raise AssertionError("slack_ack_poll should not be called")

    result = await run_ss08_gate(
        cadence=_BadCadence(),
        slack_ack_poll_fn=_never_polled,
    )
    assert result is False


# ── orchestrator wiring ───────────────────────────────────────────────────


async def test_orchestrator_gate_skips_for_non_ss08_tickets():
    """Non-SS-08 tickets: gate is a no-op returning True, no Slack calls."""
    orch = _mk_orchestrator()
    t = Ticket(
        id="u1", identifier="SAL-1", code="TIR-01",
        title="TIR-01 work", wave=1, epic="tiresias",
        size="M", estimate=5, is_critical_path=False,
    )

    ran = {"called": False}

    async def _boom(*a, **kw):
        ran["called"] = True
        raise AssertionError("run_ss08_gate should not run for non-SS-08")

    # Swap the symbol at the point of import inside _maybe_ss08_gate.
    import alfred_coo.autonomous_build.ss08_gate as gm
    # _maybe_ss08_gate does a lazy `from .ss08_gate import run_ss08_gate`,
    # so patch the attribute on the module where the lookup lands.
    orig = gm.run_ss08_gate
    gm.run_ss08_gate = _boom
    try:
        allowed = await orch._maybe_ss08_gate(t)
    finally:
        gm.run_ss08_gate = orig

    assert allowed is True
    assert ran["called"] is False


async def test_orchestrator_gate_skips_when_already_acked():
    """state.ss08_acked=True: gate returns True without any Slack traffic."""
    orch = _mk_orchestrator()
    orch.state.ss08_acked = True
    ticket = _mk_ss08_ticket()

    import alfred_coo.autonomous_build.ss08_gate as gm
    called = {"n": 0}

    async def _count_gate(**kwargs):
        called["n"] += 1
        return True

    orig = gm.run_ss08_gate
    gm.run_ss08_gate = _count_gate
    try:
        allowed = await orch._maybe_ss08_gate(ticket)
    finally:
        gm.run_ss08_gate = orig

    assert allowed is True
    assert called["n"] == 0


async def test_orchestrator_gate_acks_and_checkpoints_state(monkeypatch):
    """SS-08 ticket + ACK detected → state.ss08_acked=True, soul write happened."""
    soul = _FakeSoul()
    orch = _mk_orchestrator(soul=soul)
    ticket = _mk_ss08_ticket(is_critical_path=False)

    import alfred_coo.autonomous_build.ss08_gate as gm

    async def _ack_immediately(**kwargs):
        # Confirm orchestrator passes its own cadence + a poll callable.
        assert "cadence" in kwargs
        assert "slack_ack_poll_fn" in kwargs
        return True

    monkeypatch.setattr(gm, "run_ss08_gate", _ack_immediately)
    # Prevent the real tools.py BUILTIN_TOOLS import from mattering.
    orch._resolve_slack_ack_poll = lambda: (lambda **_kw: None)  # noqa: E731

    allowed = await orch._maybe_ss08_gate(ticket)

    assert allowed is True
    assert orch.state.ss08_acked is True
    # A soul-memory checkpoint ran after the gate cleared.
    assert any(
        "ss08_acked" in (w.get("content") or "") for w in soul.writes
    ), f"expected a checkpoint mentioning ss08_acked, got: {soul.writes}"


async def test_orchestrator_gate_on_timeout_marks_ticket_failed(monkeypatch):
    """Gate returns False → ticket.status=FAILED + event recorded."""
    soul = _FakeSoul()
    orch = _mk_orchestrator(soul=soul)
    ticket = _mk_ss08_ticket(is_critical_path=False)

    import alfred_coo.autonomous_build.ss08_gate as gm

    async def _timeout(**_kwargs):
        return False

    monkeypatch.setattr(gm, "run_ss08_gate", _timeout)
    orch._resolve_slack_ack_poll = lambda: (lambda **_kw: None)  # noqa: E731

    allowed = await orch._maybe_ss08_gate(ticket)

    assert allowed is False
    assert ticket.status == TicketStatus.FAILED
    assert orch.state.ss08_acked is False
    event_kinds = [e.get("kind") for e in orch.state.events]
    assert "ss08_gate_timeout" in event_kinds


async def test_orchestrator_gate_on_crash_marks_ticket_failed(monkeypatch):
    """run_ss08_gate raising → ticket FAILED + ss08_gate_crashed event."""
    orch = _mk_orchestrator()
    ticket = _mk_ss08_ticket(is_critical_path=False)

    import alfred_coo.autonomous_build.ss08_gate as gm

    async def _boom(**_kwargs):
        raise RuntimeError("simulated gate crash")

    monkeypatch.setattr(gm, "run_ss08_gate", _boom)
    orch._resolve_slack_ack_poll = lambda: (lambda **_kw: None)  # noqa: E731

    allowed = await orch._maybe_ss08_gate(ticket)

    assert allowed is False
    assert ticket.status == TicketStatus.FAILED
    event_kinds = [e.get("kind") for e in orch.state.events]
    assert "ss08_gate_crashed" in event_kinds


# ── misc invariants ──────────────────────────────────────────────────────


def test_cristian_slack_user_id_is_hardcoded():
    """Guard against accidental refactor that re-introduces email lookup."""
    assert CRISTIAN_SLACK_USER_ID == "U0AH88KHZ4H"


def test_ack_keywords_cover_both_phrases():
    """Sanity: regexes match the two documented reply forms."""
    import re

    for kw in ACK_KEYWORDS:
        # Every pattern is a valid regex.
        re.compile(kw, re.IGNORECASE)

    def _any_match(text: str) -> bool:
        return any(
            re.search(kw, text, re.IGNORECASE) for kw in ACK_KEYWORDS
        )

    assert _any_match("ACK SS-08")
    assert _any_match("ack ss-08")
    assert _any_match("ack ss 08")
    assert _any_match("approve SS-08")
    assert _any_match("approved ss-08")
    assert not _any_match("lgtm but later")


def test_gate_timeout_constant_is_four_hours():
    assert gate_mod.GATE_TIMEOUT_SECONDS == 4 * 3600


def test_gate_poll_interval_constant_is_two_minutes():
    assert gate_mod.GATE_POLL_INTERVAL_SECONDS == 2 * 60
