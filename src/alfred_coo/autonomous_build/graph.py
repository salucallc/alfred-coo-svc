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
    refactors."""

    PENDING = "pending"
    DISPATCHED = "dispatched"
    IN_PROGRESS = "in_progress"
    PR_OPEN = "pr_open"
    REVIEWING = "reviewing"
    MERGE_REQUESTED = "merge_requested"
    MERGED_GREEN = "merged_green"
    FAILED = "failed"
    BLOCKED = "blocked"


# Terminal states — once a ticket lands here the wave loop stops polling it.
TERMINAL_STATES: frozenset[TicketStatus] = frozenset(
    {TicketStatus.MERGED_GREEN, TicketStatus.FAILED}
)


# Epic labels we recognise. Anything outside this set lands in "other" so the
# orchestrator never silently drops a ticket. Keep in sync with plan F §4
# status-cadence (per-epic progress numbers).
KNOWN_EPICS: frozenset[str] = frozenset(
    {"tiresias", "aletheia", "fleet", "ops", "soul-gap"}
)


# Matches codes like TIR-01, ALT-03, C-26, FLEET-05, OPS-04, SS-08, AB-04
# embedded in the issue title — usually right after the `SAL-XXXX` prefix
# (but we allow it anywhere as a fallback).
_CODE_RE = re.compile(
    r"\b(TIR|ALT|C|FLEET|OPS|SS|AB|MC|SG)[-_]?(\d{1,3}[A-Za-z]?)\b",
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
    # Raw Linear state name for debugging / resume logic.
    linear_state: str = ""


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
    """Pull the short epic code (TIR-01, SS-08, ...) out of the title."""
    if not title:
        return ""
    m = _CODE_RE.search(title)
    if not m:
        return ""
    prefix = m.group(1).upper()
    number = m.group(2).upper()
    return f"{prefix}-{number}"


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
