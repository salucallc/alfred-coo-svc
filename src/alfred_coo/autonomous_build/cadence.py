"""Slack cadence for autonomous_build (AB-05).

Posts a 20-minute status summary to #batcave and fires event-driven
critical-path pings when a ticket has been stalled for more than 30
minutes. Rate-limits itself so a tight orchestrator poll loop can call
`tick()` on every iteration without flooding Slack.

Plan F section 4:
  - 20-min periodic tick with wave/tickets/in_flight/spend summary
  - critical-path ping when a CP ticket stalls >30 min
  - budget warn (80%) + drain (hard-stop) are also posted via the
    orchestrator using this module's `post()` helper
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional


logger = logging.getLogger("alfred_coo.autonomous_build.cadence")


DEFAULT_INTERVAL_MIN = 20


# Type alias for the slack_post function signature:
#   async def slack_post(message: str, channel: Optional[str] = None) -> Dict[str, Any]
SlackPostFn = Callable[..., Awaitable[Dict[str, Any]]]


def _default_slack_post_fn() -> SlackPostFn:
    """Resolve the real `slack_post` tool handler from BUILTIN_TOOLS.

    Lazy import so tests that stub out slack never pay for the tools.py
    import chain (which checks env vars on some call paths).
    """
    from alfred_coo.tools import BUILTIN_TOOLS

    spec = BUILTIN_TOOLS.get("slack_post")
    if spec is None:
        raise RuntimeError(
            "slack_post tool missing from BUILTIN_TOOLS; "
            "cannot build SlackCadence default"
        )
    return spec.handler


class SlackCadence:
    """Composes + rate-limits Slack status messages for autonomous_build.

    Public API:
      - `await tick(state, graph, budget_status)` — 20-min periodic post
      - `await critical_path_ping(ticket, elapsed_seconds, last_event)` —
        stall alert, not rate-limited (event-driven)
      - `await post(message)` — direct post (used by orchestrator for
        warn/drain Slack messages)
    """

    def __init__(
        self,
        channel: str,
        interval_minutes: int = DEFAULT_INTERVAL_MIN,
        slack_post_fn: Optional[SlackPostFn] = None,
    ) -> None:
        if not channel or not isinstance(channel, str):
            raise ValueError(f"channel must be a non-empty string (got {channel!r})")
        self.channel: str = channel
        self.interval_minutes: int = max(1, int(interval_minutes or DEFAULT_INTERVAL_MIN))
        # Resolve lazily; tests inject a fake, production uses BUILTIN_TOOLS.
        self._slack_post_fn: Optional[SlackPostFn] = slack_post_fn
        self._last_tick_ts: float = 0.0

    # resolution ------------------------------------------------------

    def _resolve_slack_post(self) -> SlackPostFn:
        if self._slack_post_fn is None:
            self._slack_post_fn = _default_slack_post_fn()
        return self._slack_post_fn

    # periodic tick ---------------------------------------------------

    async def tick(
        self,
        state: Any,
        graph: Any,
        budget_status: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Post the 20-min summary if enough time has elapsed.

        Returns the slack_post response dict on post, None if rate-limited.
        """
        now = time.time()
        interval_sec = self.interval_minutes * 60
        if self._last_tick_ts and (now - self._last_tick_ts) < interval_sec:
            return None
        self._last_tick_ts = now

        message = self._compose_tick_message(state, graph, budget_status)
        return await self.post(message)

    def _compose_tick_message(
        self,
        state: Any,
        graph: Any,
        budget_status: Dict[str, Any],
    ) -> str:
        """Build the periodic status message body.

        Format (matches plan F section 4):
            [autonomous_build] wave=<N> tickets=<green>/<total>
            in_flight=<k> spend=$<cum>/$<cap> (<pct>%)
            epics: <epic>=<g>/<t>, ...
            critical-path: <g>/<t> green
        """
        wave = getattr(state, "current_wave", "?")
        # Graph helpers (TicketGraph exposes `.tickets_in_wave(n)` and `__iter__`).
        try:
            wave_tickets = graph.tickets_in_wave(wave) if hasattr(graph, "tickets_in_wave") else []
        except Exception:
            wave_tickets = []
        total = len(wave_tickets)
        green = self._count_status(wave_tickets, "merged_green")
        in_flight = self._count_in_flight(wave_tickets)

        cum = budget_status.get("cumulative_spend_usd", 0.0) or 0.0
        cap = budget_status.get("max_usd", 0.0) or 0.0
        pct = budget_status.get("pct_spent", 0.0) or 0.0
        drain = budget_status.get("in_drain_mode", False)

        # Per-epic breakdown (wave-scoped).
        epic_counts: Dict[str, Dict[str, int]] = {}
        for t in wave_tickets:
            ec = epic_counts.setdefault(getattr(t, "epic", "?") or "?", {"g": 0, "t": 0})
            ec["t"] += 1
            if self._status_value(t) == "merged_green":
                ec["g"] += 1
        epic_line = ", ".join(
            f"{epic}={c['g']}/{c['t']}" for epic, c in sorted(epic_counts.items())
        ) or "(none)"

        # Critical-path breakdown.
        cp_tickets = [t for t in wave_tickets if getattr(t, "is_critical_path", False)]
        cp_green = self._count_status(cp_tickets, "merged_green")
        cp_total = len(cp_tickets)

        drain_note = " [DRAIN MODE]" if drain else ""

        return (
            f"[autonomous_build] wave={wave} tickets={green}/{total} "
            f"in_flight={in_flight} spend=${cum:.2f}/${cap:.2f} "
            f"({pct * 100:.0f}%){drain_note}\n"
            f"epics: {epic_line}\n"
            f"critical-path: {cp_green}/{cp_total} green"
        )

    @staticmethod
    def _status_value(ticket: Any) -> str:
        s = getattr(ticket, "status", None)
        if s is None:
            return ""
        return getattr(s, "value", str(s))

    @classmethod
    def _count_status(cls, tickets, status_value: str) -> int:
        return sum(1 for t in tickets if cls._status_value(t) == status_value)

    @classmethod
    def _count_in_flight(cls, tickets) -> int:
        in_flight_states = {
            "dispatched", "in_progress", "pr_open",
            "reviewing", "merge_requested",
        }
        return sum(1 for t in tickets if cls._status_value(t) in in_flight_states)

    # event-driven pings ---------------------------------------------

    async def critical_path_ping(
        self,
        stalled_ticket: Any,
        elapsed_seconds: int,
        last_event: str,
    ) -> Dict[str, Any]:
        """Post a stall alert for a critical-path ticket. Not rate-limited."""
        ident = getattr(stalled_ticket, "identifier", "?")
        code = getattr(stalled_ticket, "code", "") or ""
        status = self._status_value(stalled_ticket) or "?"
        mins = int(elapsed_seconds // 60)
        last_event_txt = (last_event or "").strip()[:160]
        msg = (
            f"[autonomous_build] CRITICAL-PATH STALL: {ident} {code} "
            f"stuck in status={status} for {mins} min. "
            f"Last event: {last_event_txt or '(none)'}"
        )
        return await self.post(msg)

    # direct post -----------------------------------------------------

    async def post(self, message: str) -> Dict[str, Any]:
        """Direct Slack post on this cadence's channel. Used by the
        orchestrator for budget warn/drain messages that are event-driven
        rather than periodic."""
        fn = self._resolve_slack_post()
        try:
            return await fn(message=message, channel=self.channel)
        except TypeError:
            # Some fakes accept positional args only; try that.
            return await fn(message, self.channel)

    # accessors -------------------------------------------------------

    @property
    def last_tick_ts(self) -> float:
        return self._last_tick_ts
