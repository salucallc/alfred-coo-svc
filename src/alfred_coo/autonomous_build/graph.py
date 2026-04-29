"""Ticket graph builder for the autonomous_build orchestrator.

Pulls issues + relations from Linear via the AB-03 tools, parses labels
for wave / epic / size / critical-path, and returns a `TicketGraph`
(container of `Ticket` records keyed by Linear UUID with `blocks_in` /
`blocks_out` edges populated).

The orchestrator iterates tickets wave-by-wave; within each wave it
respects `blocks_in` before dispatching.

Plan reference: F_autonomous_build_persona.md §2 (status model) + §3
(wave-gate algorithm).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence


logger = logging.getLogger("alfred_coo.autonomous_build.graph")


class TicketStatus(str, Enum):
    """Lifecycle state of one ticket in the orchestrator. Strings match the
    values in plan F §2 verbatim so soul-memory dumps are stable across
    refactors.

    SAL-2870 (2026-04-25): added ``BACKED_OFF``. A ticket transitions here
    instead of FAILED when ``retry_count < retry_budget``. After the
    configurable backoff window (`retry_backoff_sec`, default 5 min) it
    flips back to PENDING so the dispatch loop re-selects it. Sits between
    BLOCKED and FAILED semantically: not terminal, not in-flight, just
    cooling.
    """

    PENDING = "pending"
    DISPATCHED = "dispatched"
    IN_PROGRESS = "in_progress"
    PR_OPEN = "pr_open"
    REVIEWING = "reviewing"
    # Gap 3 (2026-04-29): a ticket whose PR has been handed off to a
    # Hawkman QA review task and is now waiting on the verdict. Distinct
    # from REVIEWING (which counts as in-flight for builder hard-timeout +
    # active-state reconciles): AWAITING_REVIEW is excluded from
    # ``ACTIVE_TICKET_STATES`` AND from ``_select_ready`` so the
    # deadlock-grace counter (in_flight + ready) can reach zero when a
    # wave is purely PR-pending. Set after ``_dispatch_review`` lands or
    # ``_fire_review_for_inherited_pr`` registers a review; transitions
    # back to either MERGED_GREEN (APPROVE) or DISPATCHED (REQUEST_CHANGES
    # respawn) via ``_handle_review_verdict``.
    AWAITING_REVIEW = "awaiting_review"
    MERGE_REQUESTED = "merge_requested"
    MERGED_GREEN = "merged_green"
    FAILED = "failed"
    BLOCKED = "blocked"
    BACKED_OFF = "backed_off"
    # SAL-2886 (v7p escalate-path bug, 2026-04-25 evening): the alfred-coo-a
    # persona contract has TWO valid emit-modes (persona.py:58-67):
    #   1. propose_pr -> PR URL (happy path) -> PR_OPEN -> REVIEWING -> MERGED_GREEN
    #   2. linear_create_issue -> grounding-gap issue (escalate path) -> ESCALATED
    # Prior to this state, the escalate path was misclassified as FAILED in
    # _poll_children (no PR URL -> "silent persona bug"), causing v7p wave-0
    # cascade: 4 already-merged tickets escalated, were marked FAILED, burned
    # retry budget. ESCALATED is terminal-non-failure - wave-gate excludes
    # from numerator + denominator (parity with PATH_CONFLICT excusal).
    ESCALATED = "escalated"


# Terminal states — once a ticket lands here the wave loop stops polling it.
# BACKED_OFF is intentionally NOT terminal: the deadlock detector and wave
# gate must keep waiting through the backoff window for re-dispatch.
TERMINAL_STATES: frozenset[TicketStatus] = frozenset(
    {
        TicketStatus.MERGED_GREEN,
        TicketStatus.FAILED,
        TicketStatus.ESCALATED,  # SAL-2886
    }
)


# Epic labels we recognise. Anything outside this set lands in "other" so the
# orchestrator never silently drops a ticket. Keep in sync with plan F §4
# status-cadence (per-epic progress numbers).
KNOWN_EPICS: frozenset[str] = frozenset(
    {"tiresias", "aletheia", "fleet", "ops", "soul-gap"}
)


# Matches codes like TIR-01, ALT-03, C-26, FLEET-05, OPS-04, SS-08, AB-04,
# F08, D03, E02, H01 embedded in the issue title — usually right after the
# `SAL-XXXX` prefix (but we allow it anywhere as a fallback).
#
# AB-14 (SAL-2699): widened with F/D/E/H single-letter plan-doc prefixes so
# children can grep the plan-doc markdown by ticket.code. Prior to this,
# "F08: soul-lite service..." parsed to an empty code, leaving the child
# with no grep anchor (→ scope fabrication; live-run regression on
# SAL-2616, 2026-04-23).
#
# Hint-batch-2 (SAL-3281..3288): added a letters-only branch for the
# alfred-doctor children, whose plan-doc codes are `AD-a` … `AD-h` (no
# digits — pure letter suffix). The standard `\d{1,3}[A-Za-z]?` form
# requires at least one digit and so previously skipped them, leaving 8
# wave-3 tickets in the no_hint_no_code bucket.
_CODE_RE = re.compile(
    r"\b("
    r"(?:TIR|ALT|FLEET|OPS|SS|AB|MC|SG|C|D|E|F|H)[-_]?\d{1,3}[A-Za-z]?"
    r"|AD[-_][a-h]"
    r")\b",
    re.IGNORECASE,
)


@dataclass
class Ticket:
    """One Linear issue projected into the orchestrator's schema.

    `blocks_in` / `blocks_out` hold Linear UUIDs (not identifiers) so lookups
    inside the `TicketGraph.nodes` dict are O(1). Identifiers + codes live
    on the ticket itself for logging.
    """

    id: str  # Linear UUID
    identifier: str  # "SAL-2583"
    code: str  # "TIR-01" / "SS-08" / "" if not parseable
    title: str
    wave: int  # 0..3 (parsed from `wave-N` label; -1 if unlabelled)
    epic: str  # "tiresias" / ... / "other"
    size: str  # "S" / "M" / "L" / "" — parsed from `size-*` label
    estimate: int  # Linear point estimate (0 if unset)
    is_critical_path: bool  # `critical-path` label present
    labels: List[str] = field(default_factory=list)
    blocks_in: List[str] = field(default_factory=list)  # UUIDs blocking me
    blocks_out: List[str] = field(default_factory=list)  # UUIDs I block
    status: TicketStatus = TicketStatus.PENDING
    # Populated when a child mesh task is dispatched for this ticket.
    child_task_id: Optional[str] = None
    # Populated when a PR is opened for this ticket (from child completion).
    pr_url: Optional[str] = None
    # Number of hawkman-qa-a REQUEST_CHANGES cycles seen so far.
    review_cycles: int = 0
    # AB-08: mesh task id of the most recent review dispatched for this
    # ticket. Populated by orchestrator._dispatch_review and consumed by
    # _poll_reviews to look up the verdict in the completed-tasks batch.
    review_task_id: Optional[str] = None
    # AB-08: number of times the review task completed with no readable
    # verdict (silent). Orchestrator retries once by re-firing the review,
    # then marks FAILED. Counter resets whenever a new child is respawned
    # after REQUEST_CHANGES so the retry budget only applies per review
    # attempt.
    silent_review_retries: int = 0
    # SAL-2870: per-ticket retry budget. A FAILED transition consumes one
    # ``retry_count`` slot and bounces the ticket through BACKED_OFF →
    # PENDING for re-dispatch instead of landing in terminal FAILED. The
    # default budget (2) is overrideable per-kickoff via the top-level
    # ``retry_budget`` payload field. ``retry_budget`` of 0 disables retry
    # entirely (legacy behaviour). Counter never decrements; once exhausted
    # the next FAILED is terminal.
    retry_budget: int = 2
    retry_count: int = 0
    # Wall-clock when the ticket entered BACKED_OFF. Read by
    # ``_poll_children`` on every tick to decide if the backoff window has
    # elapsed and the ticket should flip back to PENDING. ``None`` outside
    # the BACKED_OFF state.
    backed_off_at: Optional[float] = None
    # SAL-2870 (phantom-child carve-out, 2026-04-26): short tag describing
    # the most recent FAILED transition's root cause. Read by the retry-
    # budget sweep in ``_poll_children`` so the sweep can route ``phantom_*``
    # cleanups STRAIGHT to PENDING (no BACKED_OFF, no retry-count bump)
    # while real failures (silent_complete, no_pr_url, mesh-failed,
    # review_changes) still pass through the BACKED_OFF cooling window.
    # Cleared by the sweep once handled so it doesn't leak across rounds.
    # Values currently emitted by orchestrator: ``"phantom_child"`` (AB-17-x
    # force-fail) + ``"no_child_task_id"`` (AB-17-y orphan-active force-fail) +
    # ``"builder_hard_timeout"`` (sequential-discipline Fix 1: dispatched but
    # silent for >BUILDER_HARD_TIMEOUT_SEC; consumes retry budget).
    # ``None`` when the ticket has never failed in this run.
    last_failure_reason: Optional[str] = None
    # Raw Linear state name for debugging / resume logic.
    linear_state: str = ""
    # sequential-discipline Fix 1 (2026-04-28). Wall-clock when the most
    # recent ``_dispatch_child`` succeeded; consumed by ``_poll_children``'s
    # builder hard-timeout branch (force-fail w/ retry consumed when a
    # builder is dispatched but produces no completion within
    # ``BUILDER_HARD_TIMEOUT_SEC``). Cleared on terminal transition out of
    # an active state. ``None`` when no live dispatch is outstanding.
    dispatched_at: Optional[float] = None
    # sequential-discipline Fix 3 (2026-04-28). Monotonic counter of
    # ``_dispatch_child`` calls for this ticket across the run. Read by the
    # builder fallback chain in ``_dispatch_child`` to round-robin through
    # ``builder_fallback_chain``: attempt 0 → first model in chain, attempt
    # 1 → second, etc. Distinct from ``retry_count`` (which counts
    # BACKED_OFF cycles, NOT dispatches): a single retry can produce
    # multiple dispatches if a phantom-cleanup short-circuits BACKED_OFF.
    dispatch_attempts: int = 0


@dataclass
class TicketGraph:
    """Collection of `Ticket` nodes keyed by Linear UUID."""

    nodes: Dict[str, Ticket] = field(default_factory=dict)
    # Map identifier ("SAL-2583") -> UUID so lookups by identifier are cheap.
    identifier_index: Dict[str, str] = field(default_factory=dict)

    def get_by_identifier(self, identifier: str) -> Optional[Ticket]:
        uuid = self.identifier_index.get(identifier)
        return self.nodes.get(uuid) if uuid else None

    def tickets_in_wave(self, wave: int) -> List[Ticket]:
        return [t for t in self.nodes.values() if t.wave == wave]

    def all_terminal_for_wave(self, wave: int) -> bool:
        tix = self.tickets_in_wave(wave)
        return bool(tix) and all(t.status in TERMINAL_STATES for t in tix)

    def all_green_for_wave(self, wave: int) -> bool:
        tix = self.tickets_in_wave(wave)
        return bool(tix) and all(t.status == TicketStatus.MERGED_GREEN for t in tix)

    def __len__(self) -> int:
        return len(self.nodes)

    def __iter__(self):
        return iter(self.nodes.values())


# ── Label parsing helpers ───────────────────────────────────────────────────


def _parse_wave(labels: Sequence[str]) -> int:
    """Extract `wave-N` label. Returns -1 if no wave label is present."""
    for lbl in labels:
        m = re.fullmatch(r"wave-(\d+)", (lbl or "").strip().lower())
        if m:
            try:
                return int(m.group(1))
            except (TypeError, ValueError):
                continue
    return -1


def _parse_epic(labels: Sequence[str]) -> str:
    """First matching epic label wins. Falls back to "other"."""
    for lbl in labels:
        low = (lbl or "").strip().lower()
        if low in KNOWN_EPICS:
            return low
        # Some projects prefix with "epic:" — handle both forms.
        if low.startswith("epic:"):
            tail = low.split(":", 1)[1].strip()
            if tail in KNOWN_EPICS:
                return tail
    return "other"


def _parse_size(labels: Sequence[str]) -> str:
    """Extract the size-S / size-M / size-L / size-XL label."""
    for lbl in labels:
        m = re.fullmatch(r"size-(xs|s|m|l|xl)", (lbl or "").strip().lower())
        if m:
            return m.group(1).upper()
    return ""


def _parse_critical_path(labels: Sequence[str]) -> bool:
    for lbl in labels:
        if (lbl or "").strip().lower() == "critical-path":
            return True
    return False


def _parse_code(title: str) -> str:
    """Pull the short epic code (TIR-01, SS-08, F08, ...) out of the title.

    Preserves the original separator format so plan-doc greps match
    verbatim: ``TIR-01`` stays ``TIR-01`` (dash); ``F08`` stays ``F08``
    (no dash). An underscore in the source (``C_26``) is normalised to a
    dash (``C-26``) because plan docs use the dash form for multi-char
    prefixes exclusively. Uppercased for consistency.
    """
    if not title:
        return ""
    m = _CODE_RE.search(title)
    if not m:
        return ""
    return m.group(0).upper().replace("_", "-")


def _linear_state_to_status(state_name: str) -> TicketStatus:
    """Map a Linear state name back to our internal enum.

    Used by restore paths where we don't want to lose what Linear already
    knows (e.g. a ticket was manually moved to Done while the orchestrator
    was offline).
    """
    low = (state_name or "").strip().lower()
    if low in ("done", "merged", "released", "completed"):
        return TicketStatus.MERGED_GREEN
    if low in ("canceled", "cancelled", "duplicate"):
        return TicketStatus.FAILED
    if low in ("in review", "review"):
        return TicketStatus.REVIEWING
    if low in ("in progress", "started"):
        return TicketStatus.IN_PROGRESS
    if low in ("todo", "to do"):
        return TicketStatus.DISPATCHED
    # Backlog / Triage / unknown → treat as pending so the orchestrator can
    # still consider it for dispatch.
    return TicketStatus.PENDING


# ── Public loader ───────────────────────────────────────────────────────────


ListProjectIssuesFn = Callable[..., Awaitable[Dict[str, Any]]]
GetIssueRelationsFn = Callable[[str], Awaitable[Dict[str, Any]]]


async def build_ticket_graph(
    project_id: str,
    *,
    list_project_issues: ListProjectIssuesFn,
    get_issue_relations: Optional[GetIssueRelationsFn] = None,
    limit: int = 250,
) -> TicketGraph:
    """Fetch issues + relations for a Linear project and assemble a graph.

    The two fetcher callables default to the AB-03 tools
    `alfred_coo.tools.linear_list_project_issues` /
    `linear_get_issue_relations`, but are injectable so tests can stub them
    without monkeypatching module-level state.

    `linear_list_project_issues` already returns per-issue relations, so the
    second call is only needed as a backfill for issues where the relations
    list came back empty but we suspect it shouldn't be (rare — kept as a
    safety valve, disabled by default).

    Edges: we follow `blocks` (I block others) + `blocked_by` (others block
    me) symmetrically, so either side of the edge being present is enough
    to populate both `blocks_in` and `blocks_out`.
    """
    resp = await list_project_issues(project_id=project_id, limit=limit)
    if not isinstance(resp, dict):
        raise RuntimeError(
            f"linear_list_project_issues returned non-dict: {type(resp).__name__}"
        )
    if "error" in resp:
        raise RuntimeError(f"linear_list_project_issues error: {resp['error']}")
    issues: List[Dict[str, Any]] = resp.get("issues") or []
    if not issues:
        logger.warning("linear project %s returned zero issues", project_id)

    graph = TicketGraph()

    # First pass: create nodes.
    for item in issues:
        uuid = item.get("id")
        identifier = item.get("identifier") or ""
        if not uuid or not identifier:
            logger.warning("skipping issue with missing id/identifier: %r", item)
            continue
        labels = [str(lbl) for lbl in (item.get("labels") or []) if lbl]
        title = item.get("title") or ""
        state_name = ((item.get("state") or {}).get("name")) or ""
        ticket = Ticket(
            id=uuid,
            identifier=identifier,
            code=_parse_code(title),
            title=title,
            wave=_parse_wave(labels),
            epic=_parse_epic(labels),
            size=_parse_size(labels),
            estimate=int(item.get("estimate") or 0),
            is_critical_path=_parse_critical_path(labels),
            labels=labels,
            status=_linear_state_to_status(state_name),
            linear_state=state_name,
        )
        graph.nodes[uuid] = ticket
        graph.identifier_index[identifier] = uuid

    # Second pass: wire edges from the inline relations payload. Linear
    # relation types we care about:
    #   "blocks"     — the current issue blocks `relatedIssue`
    #   "blocked_by" — `relatedIssue` blocks the current issue
    for item in issues:
        uuid = item.get("id")
        if not uuid or uuid not in graph.nodes:
            continue
        me = graph.nodes[uuid]
        for rel in item.get("relations") or []:
            rtype = (rel.get("type") or "").strip().lower()
            other = rel.get("relatedIssue") or {}
            other_id = other.get("id")
            other_ident = other.get("identifier")
            # Prefer UUID lookup; fall back to identifier index if the
            # related issue is in the same project but the payload only
            # surfaced the identifier.
            if not other_id and other_ident:
                other_id = graph.identifier_index.get(other_ident)
            if not other_id or other_id not in graph.nodes:
                # Cross-project relation or not in the batch — log + skip.
                logger.debug(
                    "relation skipped: %s %s %s (other not in project batch)",
                    me.identifier, rtype, other_ident or other_id,
                )
                continue
            other_node = graph.nodes[other_id]
            if rtype == "blocks":
                if other_id not in me.blocks_out:
                    me.blocks_out.append(other_id)
                if uuid not in other_node.blocks_in:
                    other_node.blocks_in.append(uuid)
            elif rtype in ("blocked_by", "blocked-by", "blockedby"):
                if other_id not in me.blocks_in:
                    me.blocks_in.append(other_id)
                if uuid not in other_node.blocks_out:
                    other_node.blocks_out.append(uuid)
            # Other relation types (related / duplicate / ...) don't affect
            # dispatch ordering. Intentionally dropped.

    # Optional backfill: if we were given a per-issue relations fetcher AND
    # an issue in the graph has zero relations but a relation-heavy label
    # like "has-deps", re-query. This is a belt-and-braces hook the
    # orchestrator can toggle on if Linear's project-level relations payload
    # turns out to be flaky. Off by default — no-op in the common path.
    if get_issue_relations is not None:
        for ticket in list(graph.nodes.values()):
            if ticket.blocks_in or ticket.blocks_out:
                continue
            if "has-deps" not in [lbl.lower() for lbl in ticket.labels]:
                continue
            try:
                extra = await get_issue_relations(ticket.id)
            except Exception:
                logger.exception(
                    "linear_get_issue_relations failed for %s; skipping backfill",
                    ticket.identifier,
                )
                continue
            if not isinstance(extra, dict) or "error" in extra:
                continue
            for other_ident in extra.get("blocked_by") or []:
                other_id = graph.identifier_index.get(other_ident)
                if other_id and other_id not in ticket.blocks_in:
                    ticket.blocks_in.append(other_id)
            for other_ident in extra.get("blocks") or []:
                other_id = graph.identifier_index.get(other_ident)
                if other_id and other_id not in ticket.blocks_out:
                    ticket.blocks_out.append(other_id)

    logger.info(
        "ticket graph built: %d nodes, %d waves observed, %d critical-path",
        len(graph.nodes),
        len({t.wave for t in graph.nodes.values() if t.wave >= 0}),
        sum(1 for t in graph.nodes.values() if t.is_critical_path),
    )
    return graph
