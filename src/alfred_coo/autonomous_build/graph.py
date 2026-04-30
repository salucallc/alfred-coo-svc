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
    # SAL-3676 (2026-04-29): split the ``terminal-non-failure but actually
    # abandoned`` cases out of ESCALATED. Pre-fix, ``_is_wave_gate_excused``
    # axis 4 excused EVERY ESCALATED ticket regardless of why the escalation
    # fired. This masked three real abandonment classes as legitimate
    # grounding-gap escalations:
    #   - builder hard-timeout (retry budget exhausted, no PR shipping)
    #   - phantom-loop circuit breaker (child kept disappearing)
    #   - wave-stall force-pass (no green progress in 30min window)
    # Tonight (2026-04-29 03:19 UTC) the MSSP-Ext orchestrator crashed at
    # ``green=2 failed=0 excused=4 of 6 ratio=0.33`` — three of those four
    # excused were hard-timeout abandonments that should have been counted
    # as failures (and the wave-gate would have surfaced "3 failures" instead
    # of the misleading "nothing shipped" message). ABANDONED is terminal
    # AND counted in the wave-gate denominator under the FAILED column —
    # the orchestrator gave up on the ticket, that IS a failure. ESCALATED
    # is reserved for the legitimate-out-of-band cases (grounding-gap from
    # the persona's own escalate path; human-assigned dispatch-skip and the
    # respawn-path mirror at line ~6910) where the orchestrator never had a
    # workable scope.
    ABANDONED = "abandoned"


# Terminal states — once a ticket lands here the wave loop stops polling it.
# BACKED_OFF is intentionally NOT terminal: the deadlock detector and wave
# gate must keep waiting through the backoff window for re-dispatch.
TERMINAL_STATES: frozenset[TicketStatus] = frozenset(
    {
        TicketStatus.MERGED_GREEN,
        TicketStatus.FAILED,
        TicketStatus.ESCALATED,  # SAL-2886
        TicketStatus.ABANDONED,  # SAL-3676
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
#
# wave-1 silent-complete fix (2026-04-29): added MSSP-EX-X (single trailing
# letter, no digits) and MSSP-FED-W{N}-X (wave-keyed federation codes) so
# the MSSP extraction + federation tracks parse to a non-empty ticket.code
# and pick up their `_TARGET_HINTS` entries. Without these prefixes, every
# title like "MSSP-EX-A — Extract ..." or "MSSP Federation W1-A: ..."
# parsed to code='', triggering the NO_HINT (unresolved) escalation block
# and burning the wave-gate (kickoffs 0de3e2be + dae5a5c0, both crashed
# 2026-04-29 with green=0/excused=N).
#
# wave-1 silent-complete fix follow-up (2026-04-29 evening): added CO-W{N}-X
# (Cockpit Consumer UX) and AI-W{N}-X (Agent Ingest) wave-keyed codes so
# SAL-3591/3592/3593 (Cockpit-UX wave-1) and SAL-3609/3610/3611/3612
# (Agent-Ingest wave-1) parse to a non-empty ticket.code and pick up their
# `_TARGET_HINTS` entries. Cockpit titles use the long form
# "[Cockpit Consumer UX W1-A] ..." which `_parse_code` normalises before
# regex search; Agent-Ingest titles use the bare "[W1-A] ..." form, so
# `_parse_code` discriminates by inspecting the ticket's labels (presence
# of `track:agent-ingest` triggers the AI- normalisation) — see callers
# in `build_ticket_graph`. Mirrors the PR #302 pattern.
_CODE_RE = re.compile(
    r"\b("
    r"(?:TIR|ALT|FLEET|OPS|SS|AB|MC|SG|C|D|E|F|H)[-_]?\d{1,3}[A-Za-z]?"
    r"|AD[-_][a-h]"
    r"|MSSP[-_]EX[-_][A-Za-z]"
    r"|MSSP[-_]FED[-_]W\d[-_][A-Za-z]"
    r"|CO[-_]W\d[-_][A-Za-z]"
    r"|AI[-_]W\d[-_][A-Za-z]"
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
    # dynamic-hints-from-ticket-body refactor (2026-04-29): cache the raw
    # Linear ticket body on the graph node so the orchestrator's hint
    # resolver can re-parse the ``## Target`` block at dispatch time
    # without re-querying Linear. Empty string when the ticket has no
    # description (rare but legal). Kept off ``__repr__`` cost-paths via
    # the dataclass default so existing code that prints tickets still
    # produces stable output (default value only — no field flag needed).
    body: str = ""
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


def _parse_code(title: str, labels: Optional[Sequence[str]] = None) -> str:
    """Pull the short epic code (TIR-01, SS-08, F08, ...) out of the title.

    Preserves the original separator format so plan-doc greps match
    verbatim: ``TIR-01`` stays ``TIR-01`` (dash); ``F08`` stays ``F08``
    (no dash). An underscore in the source (``C_26``) is normalised to a
    dash (``C-26``) because plan docs use the dash form for multi-char
    prefixes exclusively. Uppercased for consistency.

    wave-1 silent-complete fix (2026-04-29): the MSSP federation track
    titles use "MSSP Federation W1-A: ..." (a SPACE between MSSP and
    Federation), which `_CODE_RE` can't match as a single token. We
    pre-normalise that specific phrase to ``MSSP-FED-W1-A`` before the
    regex search so federation tickets parse to a non-empty code and
    pick up their `_TARGET_HINTS` entries instead of NO_HINT-escalating
    every wave-1 builder. The MSSP-EX-A style already uses dashes and
    matches directly via the new regex branch.

    wave-1 silent-complete fix follow-up (2026-04-29 evening): the
    Cockpit Consumer UX track uses titles like
    "[Cockpit Consumer UX W1-A] ..." (long form with the track name
    inline), and the Agent Ingest track uses the bare "[W1-A] ..." form.
    To disambiguate Agent-Ingest's bare bracket prefix from any other
    "[W1-A]" usage that might appear elsewhere (today there is none, but
    we don't want to be a hostage to that), we accept an optional
    ``labels`` argument and normalise "[W<N>-<X>]" -> "AI-W<N>-<X>" only
    when ``track:agent-ingest`` is present. Cockpit's long-form prefix
    is unambiguous and is normalised unconditionally.
    """
    if not title:
        return ""
    # Normalise "MSSP Federation W<N>-<X>" -> "MSSP-FED-W<N>-<X>" so the
    # regex can extract a single token. Case-insensitive, single pass.
    normalised = re.sub(
        r"\bMSSP\s+Federation\s+(W\d+[-_][A-Za-z])\b",
        r"MSSP-FED-\1",
        title,
        flags=re.IGNORECASE,
    )
    # Normalise "[Cockpit Consumer UX W<N>-<X>]" -> "CO-W<N>-<X>" so the
    # regex can extract the cockpit-track wave code as a single token.
    # The long-form bracket prefix is unambiguous and unique to this
    # track, so this normalisation is unconditional.
    normalised = re.sub(
        r"\[\s*Cockpit\s+Consumer\s+UX\s+W(\d+)[-_]([A-Za-z])\s*\]",
        r"CO-W\1-\2",
        normalised,
        flags=re.IGNORECASE,
    )
    # Normalise bare "[W<N>-<X>]" -> "AI-W<N>-<X>" ONLY when this ticket
    # carries the ``track:agent-ingest`` label. Without the label-gate
    # this regex would over-match unrelated tickets that happen to use a
    # bare wave bracket; gating on the track label keeps the
    # normalisation surgical to the Agent-Ingest project.
    label_set = {(lbl or "").strip().lower() for lbl in (labels or [])}
    if "track:agent-ingest" in label_set:
        normalised = re.sub(
            r"\[\s*W(\d+)[-_]([A-Za-z])\s*\]",
            r"AI-W\1-\2",
            normalised,
            flags=re.IGNORECASE,
        )
    m = _CODE_RE.search(normalised)
    if not m:
        return ""
    return m.group(0).upper().replace("_", "-")


#: Target-hint parser (dynamic-hints-from-ticket-body refactor, 2026-04-29).
#:
#: Matches a ``## Target`` (or ``## TARGET``) markdown header at the start
#: of a line, then captures everything up to the next top-level ``##``
#: header or end-of-string. Used by ``_parse_target_from_ticket_body`` to
#: pluck the embedded YAML-ish target block out of a Linear ticket body.
_TARGET_BLOCK_RE = re.compile(
    r"(?im)^\#\#\s*Target\s*\n(?P<body>.*?)(?=^\#\#\s|\Z)",
    re.DOTALL | re.MULTILINE,
)

# Recognise ``key: value`` lines in the target block.
_TARGET_KV_RE = re.compile(r"^\s*(?P<key>[a-zA-Z_]+)\s*:\s*(?P<val>.*?)\s*$")

# Recognise list items under ``paths:`` / ``new_paths:`` (``  - some/path``
# or ``* some/path``). Linear's markdown renderer round-trips bullets
# inconsistently — POSTed ``  - foo`` may come back as ``* foo`` after
# a save-load cycle through the web UI, so we accept either marker.
_TARGET_LIST_ITEM_RE = re.compile(r"^\s*[-*]\s+(?P<item>.+?)\s*$")

# Codes that should NEVER come back from a parsed body (they're sentinel
# placeholders the planner sub may emit for genuinely unresolved tickets).
# The parser returns ``None`` when it sees them so the resolver falls
# through to the registry / NO_HINT path cleanly.
_TARGET_UNRESOLVED_MARKERS = frozenset(
    {"unresolved", "(unresolved)", "tbd", "see plan doc", "(unresolved — see plan doc)"}
)


def _parse_target_from_ticket_body(body: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse a ``## Target`` markdown block out of a Linear ticket body.

    Schema (parser-friendly markdown — keep it strict so the planner sub
    can emit it deterministically):

    .. code-block:: markdown

        ## Target

        owner: salucallc
        repo: alfred-coo-svc
        paths:
          - src/alfred_coo/cockpit_router.py
          - tests/test_cockpit_router.py
        new_paths:
          - migrations/0042_consent_grants.sql
        base_branch: main
        branch_hint: feature/sal-XXXX-short-slug
        notes: free-form notes for the builder

    Returns a dict with the same shape as ``orchestrator.TargetHint``
    (kwargs-compatible: ``owner``, ``repo``, ``paths``, ``new_paths``,
    ``base_branch``, ``branch_hint``, ``notes``) so the orchestrator can
    construct a ``TargetHint`` directly. Returns ``None`` when:

    * ``body`` is empty or has no ``## Target`` section;
    * the section is the ``(unresolved — ...)`` placeholder; or
    * the parsed block lacks both ``owner`` and ``repo`` (the bare
      minimum every hint must have to be useful).

    Both ``paths`` and ``new_paths`` are returned as tuples (or omitted
    when absent) so the dataclass invariant
    (``paths`` ∪ ``new_paths`` non-empty) enforces "useful hint" at
    construction time. ``base_branch`` defaults to ``"main"`` to match
    the ``TargetHint`` dataclass default.

    The parser is intentionally lenient about whitespace + indentation
    (so tickets pasted from Linear's web UI parse the same as those
    POSTed via the API) but strict about the key vocabulary — unknown
    keys are dropped silently. ``paths`` / ``new_paths`` lists keep YAML
    semantics: a ``- item`` line under either key adds to that list
    until the next non-list, non-blank line.
    """
    if not body:
        return None

    m = _TARGET_BLOCK_RE.search(body)
    if not m:
        return None

    block = m.group("body") or ""
    # Quick sniff for the placeholder. The planner sub may emit
    # ``## Target\n(unresolved — see plan doc)`` for tickets whose
    # target is genuinely undecided; treat that as "no body hint" so
    # the resolver falls through to registry/NO_HINT cleanly.
    stripped_first = ""
    for line in block.splitlines():
        if line.strip():
            stripped_first = line.strip().lower()
            break
    if stripped_first.startswith("(unresolved") or stripped_first in _TARGET_UNRESOLVED_MARKERS:
        return None

    parsed: Dict[str, Any] = {}
    paths: List[str] = []
    new_paths: List[str] = []
    current_list: Optional[List[str]] = None

    for raw in block.splitlines():
        line = raw.rstrip()
        if not line.strip():
            # Blank line: do NOT end list-collection mode. Linear's
            # markdown renderer inserts a blank line between ``paths:``
            # and the first ``* item`` after a save-load round-trip
            # (the renderer normalises tight lists to loose lists with a
            # paragraph break). Keep ``current_list`` armed; only a
            # subsequent non-list, non-blank line ends list mode.
            continue

        # List item under paths: / new_paths:
        if current_list is not None:
            li = _TARGET_LIST_ITEM_RE.match(line)
            if li:
                item = li.group("item").strip()
                # Drop trailing inline comments (`# verified exists @ main` etc.)
                if "#" in item:
                    # Only strip when the # is preceded by whitespace —
                    # don't mangle filenames that legitimately contain #.
                    hash_match = re.search(r"\s+#", item)
                    if hash_match:
                        item = item[: hash_match.start()].rstrip()
                if item:
                    current_list.append(item)
                continue
            # Non-list line ends list mode.
            current_list = None

        kv = _TARGET_KV_RE.match(line)
        if not kv:
            continue
        key = kv.group("key").strip().lower()
        val = (kv.group("val") or "").strip()

        if key in ("paths", "new_paths"):
            current_list = paths if key == "paths" else new_paths
            # YAML allows inline values too (``paths: [a, b]``) but we
            # don't emit that form; if val is non-empty assume it's a
            # single-item shorthand.
            if val and not val.startswith("["):
                current_list.append(val)
            continue

        if key in ("owner", "repo", "base_branch", "branch_hint", "notes"):
            parsed[key] = val
            continue

        # Unknown key — silently dropped (forward-compat).

    # Minimum viable hint: owner + repo present.
    if not parsed.get("owner") or not parsed.get("repo"):
        return None

    if paths:
        parsed["paths"] = tuple(paths)
    if new_paths:
        parsed["new_paths"] = tuple(new_paths)

    # Default base_branch to "main" to match the TargetHint dataclass
    # default — but only if we don't already have one in the body.
    parsed.setdefault("base_branch", "main")

    # At least one of paths / new_paths must be non-empty (TargetHint
    # invariant). If the body lists neither, the planner sub goofed; the
    # resolver should fall through to registry/NO_HINT instead of
    # crashing the dataclass constructor.
    if not parsed.get("paths") and not parsed.get("new_paths"):
        return None

    # Drop empty optional strings so TargetHint(...)'s defaults apply.
    for opt_key in ("branch_hint", "notes"):
        if not parsed.get(opt_key):
            parsed.pop(opt_key, None)

    return parsed


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
            code=_parse_code(title, labels=labels),
            title=title,
            wave=_parse_wave(labels),
            epic=_parse_epic(labels),
            size=_parse_size(labels),
            estimate=int(item.get("estimate") or 0),
            is_critical_path=_parse_critical_path(labels),
            labels=labels,
            status=_linear_state_to_status(state_name),
            linear_state=state_name,
            # dynamic-hints-from-ticket-body refactor: cache the raw body
            # so the orchestrator's hint resolver can re-parse the
            # ``## Target`` section at dispatch time without an extra
            # Linear round-trip.  Linear surfaces it as ``description``.
            body=str(item.get("description") or ""),
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
