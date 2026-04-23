"""AB-05 tests: SlackCadence — periodic tick + critical-path ping + post()."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import pytest

from alfred_coo.autonomous_build.cadence import SlackCadence
from alfred_coo.autonomous_build.graph import (
    Ticket,
    TicketGraph,
    TicketStatus,
)
from alfred_coo.autonomous_build.state import OrchestratorState


# ── helpers ────────────────────────────────────────────────────────────────


class _SpySlackPost:
    """Records every call. Matches the real `slack_post` signature:
    `async def slack_post(message: str, channel: Optional[str] = None)`.
    """

    def __init__(self, response: Optional[Dict[str, Any]] = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._response = response or {"ts": "1.0", "channel": "C0ASAKFTR1C"}

    async def __call__(
        self,
        message: str,
        channel: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.calls.append({"message": message, "channel": channel})
        return self._response


def _t(uuid, ident, code, wave, epic, **kwargs) -> Ticket:
    return Ticket(
        id=uuid, identifier=ident, code=code, title=f"{ident} {code}",
        wave=wave, epic=epic,
        size=kwargs.pop("size", "M"),
        estimate=kwargs.pop("estimate", 5),
        is_critical_path=kwargs.pop("is_critical_path", False),
        **kwargs,
    )


def _seed_graph(tickets: List[Ticket]) -> TicketGraph:
    g = TicketGraph()
    for t in tickets:
        g.nodes[t.id] = t
        g.identifier_index[t.identifier] = t.id
    return g


def _mk_state(wave: int = 0) -> OrchestratorState:
    s = OrchestratorState(kickoff_task_id="kick-1")
    s.current_wave = wave
    return s


# ── constructor guards ───────────────────────────────────────────────────


def test_constructor_rejects_empty_channel():
    with pytest.raises(ValueError):
        SlackCadence(channel="")


def test_constructor_coerces_bad_interval_to_minimum_one():
    # Plan calls for minimum 1 min so test harnesses can still move.
    c = SlackCadence(channel="C0", interval_minutes=0, slack_post_fn=_SpySlackPost())
    assert c.interval_minutes == 1


# ── tick rate limiting ───────────────────────────────────────────────────


def test_tick_posts_first_call():
    spy = _SpySlackPost()
    c = SlackCadence(channel="C0", interval_minutes=20, slack_post_fn=spy)
    state = _mk_state()
    graph = _seed_graph([_t("ua", "SAL-1", "X-1", 0, "ops")])
    budget_status = {
        "cumulative_spend_usd": 3.2,
        "max_usd": 30.0,
        "pct_spent": 0.1067,
        "in_drain_mode": False,
    }
    asyncio.run(c.tick(state, graph, budget_status))
    assert len(spy.calls) == 1


def test_tick_rate_limits_to_interval():
    """Within the configured interval the cadence should suppress posts."""
    spy = _SpySlackPost()
    c = SlackCadence(channel="C0", interval_minutes=20, slack_post_fn=spy)
    state = _mk_state()
    graph = _seed_graph([])
    bs = {"cumulative_spend_usd": 0.0, "max_usd": 30.0,
          "pct_spent": 0.0, "in_drain_mode": False}

    asyncio.run(c.tick(state, graph, bs))
    asyncio.run(c.tick(state, graph, bs))
    asyncio.run(c.tick(state, graph, bs))
    # Only the first call posts.
    assert len(spy.calls) == 1


def test_tick_posts_again_after_interval_elapsed():
    spy = _SpySlackPost()
    c = SlackCadence(channel="C0", interval_minutes=20, slack_post_fn=spy)
    state = _mk_state()
    graph = _seed_graph([])
    bs = {"cumulative_spend_usd": 0.0, "max_usd": 30.0,
          "pct_spent": 0.0, "in_drain_mode": False}

    asyncio.run(c.tick(state, graph, bs))
    # Backdate the last tick by > interval.
    c._last_tick_ts = time.time() - (25 * 60)
    asyncio.run(c.tick(state, graph, bs))
    assert len(spy.calls) == 2


# ── message composition ─────────────────────────────────────────────────


def test_tick_composes_message_with_waves_and_spend():
    spy = _SpySlackPost()
    c = SlackCadence(channel="C0ASAKFTR1C", interval_minutes=20, slack_post_fn=spy)
    state = _mk_state(wave=1)
    a = _t("ua", "SAL-1", "TIR-01", 1, "tiresias", is_critical_path=True)
    b = _t("ub", "SAL-2", "TIR-02", 1, "tiresias")
    c_t = _t("uc", "SAL-3", "OPS-01", 1, "ops")
    a.status = TicketStatus.MERGED_GREEN
    b.status = TicketStatus.IN_PROGRESS
    c_t.status = TicketStatus.PENDING
    graph = _seed_graph([a, b, c_t])
    bs = {
        "cumulative_spend_usd": 12.50,
        "max_usd": 30.0,
        "pct_spent": 0.4167,
        "in_drain_mode": False,
    }

    asyncio.run(c.tick(state, graph, bs))
    assert spy.calls, "expected one post"
    msg = spy.calls[0]["message"]
    assert "wave=1" in msg
    assert "tickets=1/3" in msg
    assert "in_flight=1" in msg
    assert "$12.50" in msg
    assert "$30.00" in msg
    assert "41%" in msg or "42%" in msg
    # Epics breakdown present.
    assert "tiresias=1/2" in msg
    assert "ops=0/1" in msg
    # Critical-path tally present.
    assert "critical-path: 1/1 green" in msg
    # Channel passed through.
    assert spy.calls[0]["channel"] == "C0ASAKFTR1C"


def test_tick_shows_drain_mode_annotation():
    spy = _SpySlackPost()
    c = SlackCadence(channel="C0", interval_minutes=20, slack_post_fn=spy)
    state = _mk_state()
    graph = _seed_graph([])
    bs = {"cumulative_spend_usd": 30.0, "max_usd": 30.0,
          "pct_spent": 1.0, "in_drain_mode": True}

    asyncio.run(c.tick(state, graph, bs))
    assert "DRAIN MODE" in spy.calls[0]["message"]


# ── critical-path ping ───────────────────────────────────────────────────


def test_critical_path_ping_posts_stall_notice():
    spy = _SpySlackPost()
    c = SlackCadence(channel="C0", interval_minutes=20, slack_post_fn=spy)
    t = _t("ua", "SAL-42", "CP-01", 0, "tiresias", is_critical_path=True)
    t.status = TicketStatus.IN_PROGRESS

    asyncio.run(c.critical_path_ping(t, elapsed_seconds=45 * 60,
                                     last_event="ticket_dispatched"))
    assert len(spy.calls) == 1
    msg = spy.calls[0]["message"]
    assert "CRITICAL-PATH STALL" in msg
    assert "SAL-42" in msg
    assert "CP-01" in msg
    assert "45 min" in msg
    assert "ticket_dispatched" in msg


def test_critical_path_ping_not_rate_limited():
    """Stall pings are event-driven; two in a row both post."""
    spy = _SpySlackPost()
    c = SlackCadence(channel="C0", interval_minutes=20, slack_post_fn=spy)
    t = _t("ua", "SAL-42", "CP-01", 0, "ops", is_critical_path=True)

    asyncio.run(c.critical_path_ping(t, elapsed_seconds=1800, last_event="x"))
    asyncio.run(c.critical_path_ping(t, elapsed_seconds=3600, last_event="y"))
    assert len(spy.calls) == 2


# ── direct post ─────────────────────────────────────────────────────────


def test_post_forwards_to_slack_post_fn_with_channel():
    spy = _SpySlackPost()
    c = SlackCadence(channel="C-BATCAVE", interval_minutes=20, slack_post_fn=spy)
    asyncio.run(c.post(":stop_sign: hard stop"))
    assert spy.calls == [{"message": ":stop_sign: hard stop", "channel": "C-BATCAVE"}]


def test_post_falls_back_to_positional_on_typeerror():
    """Some fakes accept only positional args; cadence must tolerate that."""

    class _PositionalOnly:
        def __init__(self) -> None:
            self.calls: list = []

        async def __call__(self, message, channel):  # no kwargs allowed
            self.calls.append((message, channel))
            return {"ts": "1"}

    f = _PositionalOnly()
    c = SlackCadence(channel="C-X", interval_minutes=20, slack_post_fn=f)
    asyncio.run(c.post("hello"))
    assert f.calls == [("hello", "C-X")]
