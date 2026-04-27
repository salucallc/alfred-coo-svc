"""Orchestrator state container + soul-memory checkpoint/restore.

The orchestrator persists a structured JSON snapshot to soul memory on
every loop iteration. On daemon restart the orchestrator reads the most
recent snapshot back and resumes where it left off — wave index, per-
ticket status map, cumulative spend, and flags like `ss08_acked`.

Plan F §1 R2: state lives in soul memory, not a bespoke Supabase table.
Topic convention: `autonomous_build:<kickoff_task_id>:state`.

In addition to per-kickoff state snapshots, this module also persists
*wave-pass* records keyed by ``(linear_project_id, wave_n)``. These are
read on wave entry by ``AutonomousBuildOrchestrator`` to short-circuit
re-evaluation of waves that already landed at ratio=1.00 in a recent run
(see Fix A in PR ``feat/wave-skip-and-stale-state-sweeper``).

Wave-pass topic convention:
    ``autonomous_build:wave_pass:<linear_project_id>:wave_<n>``

Schema is JSON-serialised into the memory ``content`` field (per soul-svc
v2.0.0 ``/v1/memory/write``). Reads use ``/v1/memory/recent?topics=...``
and pick the freshest record (soul-svc returns reverse-chronological).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


logger = logging.getLogger("alfred_coo.autonomous_build.state")


# Topic prefix — one entry per kickoff id keeps parallel runs isolated.
STATE_TOPIC_PREFIX = "autonomous_build"
STATE_TOPIC_SUFFIX = "state"

# Wave-pass topic suffix. Distinct from the per-kickoff state topic so a
# `recent_memories(topics=[wave_pass_topic_for(...)])` lookup never has to
# scan through state snapshots.
WAVE_PASS_TOPIC_INFIX = "wave_pass"

# Default freshness window for a persisted 1.00 wave-pass to count as
# "recent enough to skip" on re-entry. 24h matches the daily kickoff
# cadence — anything older should be re-evaluated to catch overnight
# drift in Linear or GitHub state.
WAVE_PASS_FRESHNESS_SEC = 24 * 60 * 60

# Gate-ACK topic infix. Persists Cristian-approved gate ACKs (e.g. SS-08)
# keyed by ``(linear_project_id, gate_name)`` so a daemon restart inside
# the same project does not re-prompt for an ACK already given.
GATE_ACK_TOPIC_INFIX = "gate_ack"

# Default freshness window for a gate ACK. 30 days is generous: gates are
# tied to a specific spec revision and the project lifecycle is typically
# days-to-weeks. Anything older should re-prompt on the chance the spec
# drifted.
GATE_ACK_FRESHNESS_SEC = 30 * 24 * 60 * 60


def state_topic_for(kickoff_task_id: str) -> str:
    return f"{STATE_TOPIC_PREFIX}:{kickoff_task_id}:{STATE_TOPIC_SUFFIX}"


def wave_pass_topic_for(linear_project_id: str, wave_n: int) -> str:
    """Topic key for a wave-pass record.

    Keyed by ``(linear_project_id, wave_n)`` rather than ``kickoff_task_id``
    so the cache survives kickoff-task churn — a fresh kickoff for the same
    project should still benefit from yesterday's pass record. Wave index
    is appended verbatim so each wave gets its own record.
    """
    return (
        f"{STATE_TOPIC_PREFIX}:{WAVE_PASS_TOPIC_INFIX}:"
        f"{linear_project_id}:wave_{wave_n}"
    )


def gate_ack_topic_for(linear_project_id: str, gate_name: str) -> str:
    """Topic key for a persisted gate-ACK record.

    Keyed by ``(linear_project_id, gate_name)`` so the ACK survives daemon
    restarts within the same Linear project lifecycle. A fresh kickoff for
    the same project will skip the gate poll entirely if a recent ACK is
    on file. The gate name (e.g. ``"SS-08"``) is appended verbatim so each
    gate gets its own record — orchestrators with multiple gates do not
    cross-pollute.
    """
    return (
        f"{STATE_TOPIC_PREFIX}:{GATE_ACK_TOPIC_INFIX}:"
        f"{linear_project_id}:{gate_name}"
    )


@dataclass
class WavePassRecord:
    """Persisted result of a wave-gate pass.

    Written by the orchestrator after a successful wave gate (ratio=1.00
    only — soft-greens are NOT cached because they imply at least one
    failure that should be re-checked next run). Read on wave entry by
    ``_should_skip_wave`` to decide whether to bypass dispatch+gate.

    ``ticket_codes_seen`` snapshots the wave's ticket identifier set at
    pass time. On re-entry the orchestrator compares it against the
    current Linear graph; if any ticket has been removed or moved
    backward (e.g. Done -> Backlog), the cache is invalidated.
    """

    linear_project_id: str
    wave_n: int
    ratio: float
    passed_at: str  # ISO-8601 UTC, "Z"-suffixed
    denominator: int
    green_count: int
    ticket_codes_seen: List[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, blob: str) -> "WavePassRecord":
        data = json.loads(blob)
        # Forward-compat: drop unknown keys rather than erroring.
        known = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


@dataclass
class OrchestratorState:
    """Round-trippable snapshot of orchestrator progress.

    `ticket_status` maps Linear UUID -> status string (the `.value` of
    `TicketStatus`). Stored as a flat dict so the JSON serialised into
    soul memory stays small + diff-friendly.

    `dispatched_child_tasks` maps Linear UUID -> mesh task id of the most
    recent child the orchestrator spawned for that ticket. Used on restart
    to reconcile orphans.
    """

    kickoff_task_id: str
    current_wave: int = 0
    ticket_status: Dict[str, str] = field(default_factory=dict)
    dispatched_child_tasks: Dict[str, str] = field(default_factory=dict)
    pr_urls: Dict[str, str] = field(default_factory=dict)
    review_cycles: Dict[str, int] = field(default_factory=dict)
    # AB-08: REVIEWING → MERGED_GREEN loop bookkeeping.
    #   review_task_ids  ticket_uuid → hawkman-qa-a mesh task id
    #   review_verdicts  ticket_uuid → last verdict (APPROVE / REQUEST_CHANGES / ...)
    #   merged_pr_urls   ticket_uuid → merge_commit_sha (or pr_url fallback)
    # All three survive to_json/from_json; unknown keys already drop on load
    # so old snapshots keep loading forward-compat.
    review_task_ids: Dict[str, str] = field(default_factory=dict)
    review_verdicts: Dict[str, str] = field(default_factory=dict)
    merged_pr_urls: Dict[str, str] = field(default_factory=dict)
    cumulative_spend_usd: float = 0.0
    ss08_acked: bool = False
    last_cadence_ts: float = 0.0
    last_checkpoint_ts: float = 0.0
    # SAL-2870 retry-budget bookkeeping. Snapshot/restore parity for the
    # per-ticket retry counter and BACKED_OFF wall-clock so a daemon
    # restart inside the cooling window resumes the same backoff timer
    # rather than starting fresh.
    retry_counts: Dict[str, int] = field(default_factory=dict)
    backed_off_at: Dict[str, float] = field(default_factory=dict)
    # Deadlock-grace tracker — single float, not per-ticket. Persists so a
    # daemon bounce mid-grace-window does not reset the timer (would let
    # the orchestrator coerce immediately on restart). ``None`` means no
    # active no-progress streak.
    no_progress_since: Optional[float] = None
    # Free-form event log — capped to keep memory writes bounded.
    events: List[Dict[str, Any]] = field(default_factory=list)

    MAX_EVENTS: int = 50

    def record_event(self, kind: str, **payload: Any) -> None:
        evt = {"ts": time.time(), "kind": kind, **payload}
        self.events.append(evt)
        if len(self.events) > self.MAX_EVENTS:
            # Keep the tail — most recent events matter most on restart.
            self.events = self.events[-self.MAX_EVENTS :]

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=False, default=str)

    @classmethod
    def from_json(cls, blob: str) -> "OrchestratorState":
        data = json.loads(blob)
        # Drop unknown keys rather than erroring — forward compat when AB-05
        # adds new fields (e.g. per-model token counters).
        known = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


async def checkpoint(
    state: OrchestratorState,
    soul_client,
    kickoff_task_id: str,
) -> Optional[Dict[str, Any]]:
    """Serialise `state` and write it to soul memory under the canonical
    topic for this kickoff. Returns the soul-svc response dict on success,
    or None on failure (failures are logged and swallowed — checkpoint
    failure must not crash the orchestrator)."""
    if soul_client is None:
        logger.debug("checkpoint skipped: soul_client is None (dry-run?)")
        return None
    state.last_checkpoint_ts = time.time()
    topic = state_topic_for(kickoff_task_id)
    try:
        blob = state.to_json()
    except (TypeError, ValueError):
        logger.exception("failed to serialise orchestrator state")
        return None
    try:
        return await soul_client.write_memory(
            blob,
            topics=[topic, STATE_TOPIC_PREFIX, kickoff_task_id],
        )
    except Exception:
        logger.exception("soul write_memory failed during checkpoint")
        return None


async def restore(
    soul_client,
    kickoff_task_id: str,
) -> Optional[OrchestratorState]:
    """Look up the most recent state snapshot for `kickoff_task_id` in
    soul memory. Returns None if nothing is found or if the lookup fails.

    We rely on `SoulClient.recent_memories(topics=[<state_topic>])` and
    pick the newest entry. soul-svc returns memories in reverse-chronological
    order so the first match (after dict/list unwrapping) is the freshest
    checkpoint.
    """
    if soul_client is None:
        return None
    topic = state_topic_for(kickoff_task_id)
    try:
        recent = await soul_client.recent_memories(limit=5, topics=[topic])
    except Exception:
        logger.exception("soul recent_memories failed during restore")
        return None

    # soul-svc might return either a list or a dict with a "memories" key.
    if isinstance(recent, dict):
        recent = recent.get("memories") or []
    if not isinstance(recent, list) or not recent:
        return None

    for mem in recent:
        content = (mem or {}).get("content") if isinstance(mem, dict) else None
        if not content:
            continue
        try:
            state = OrchestratorState.from_json(content)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning(
                "skipping malformed state snapshot for %s", kickoff_task_id
            )
            continue
        # Defensive: only accept snapshots that match the kickoff id we
        # asked for. A topic collision shouldn't happen, but belt-and-braces.
        if state.kickoff_task_id and state.kickoff_task_id != kickoff_task_id:
            logger.warning(
                "ignored state snapshot with mismatched kickoff id %s (wanted %s)",
                state.kickoff_task_id, kickoff_task_id,
            )
            continue
        return state
    return None


# ── Wave-pass cache (Fix A) ────────────────────────────────────────────────
#
# The orchestrator currently re-evaluates every wave from scratch on each
# kickoff/restart. For waves that previously landed at ratio=1.00 with no
# new Linear churn, this is pure waste: hint verification + dispatch loop
# burn cycles only to re-confirm the existing all-green state.
#
# These two helpers persist the pass result keyed by
# ``(linear_project_id, wave_n)`` so a re-entered orchestrator can short-
# circuit the wave when the prior pass is still valid. See
# ``AutonomousBuildOrchestrator._should_skip_wave`` for the consumer side.


async def record_wave_pass(
    soul_client,
    *,
    linear_project_id: str,
    wave_n: int,
    ratio: float,
    denominator: int,
    green_count: int,
    ticket_codes_seen: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Persist a wave-pass record under the canonical wave-pass topic.

    Only call this on a TRUE all-green pass (ratio == 1.00) — soft-greens
    are deliberately NOT cached because they carry at least one failure
    that should be re-evaluated next run.

    Failures are logged + swallowed; cache miss is the safe fallback (the
    orchestrator will simply re-evaluate the wave next time).
    """
    if soul_client is None:
        logger.debug("record_wave_pass skipped: soul_client is None (dry-run?)")
        return None
    if not linear_project_id:
        logger.debug("record_wave_pass skipped: empty linear_project_id")
        return None
    record = WavePassRecord(
        linear_project_id=linear_project_id,
        wave_n=int(wave_n),
        ratio=float(ratio),
        passed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        denominator=int(denominator),
        green_count=int(green_count),
        ticket_codes_seen=sorted(ticket_codes_seen or []),
    )
    topic = wave_pass_topic_for(linear_project_id, wave_n)
    try:
        blob = record.to_json()
    except (TypeError, ValueError):
        logger.exception("failed to serialise wave-pass record")
        return None
    try:
        return await soul_client.write_memory(
            blob,
            topics=[topic, STATE_TOPIC_PREFIX, WAVE_PASS_TOPIC_INFIX],
        )
    except Exception:
        logger.exception(
            "soul write_memory failed during record_wave_pass "
            "(project=%s wave=%d)", linear_project_id, wave_n,
        )
        return None


async def lookup_wave_pass(
    soul_client,
    *,
    linear_project_id: str,
    wave_n: int,
) -> Optional[WavePassRecord]:
    """Fetch the most recent wave-pass record for ``(project, wave)``.

    Returns ``None`` if no record exists, the record is malformed, or the
    soul lookup fails. Returns the parsed ``WavePassRecord`` otherwise —
    the *caller* is responsible for checking ``passed_at`` freshness and
    reconciling ``ticket_codes_seen`` against the live graph.
    """
    if soul_client is None or not linear_project_id:
        return None
    topic = wave_pass_topic_for(linear_project_id, wave_n)
    try:
        recent = await soul_client.recent_memories(limit=5, topics=[topic])
    except Exception:
        logger.exception(
            "soul recent_memories failed during lookup_wave_pass "
            "(project=%s wave=%d)", linear_project_id, wave_n,
        )
        return None
    if isinstance(recent, dict):
        recent = recent.get("memories") or []
    if not isinstance(recent, list) or not recent:
        return None
    for mem in recent:
        content = (mem or {}).get("content") if isinstance(mem, dict) else None
        if not content:
            continue
        try:
            record = WavePassRecord.from_json(content)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning(
                "skipping malformed wave-pass record (project=%s wave=%d)",
                linear_project_id, wave_n,
            )
            continue
        # Belt-and-braces: a topic collision shouldn't happen, but reject
        # records whose embedded keys disagree with our query.
        if record.linear_project_id and record.linear_project_id != linear_project_id:
            logger.warning(
                "ignored wave-pass record with mismatched project id %s "
                "(wanted %s)", record.linear_project_id, linear_project_id,
            )
            continue
        if int(record.wave_n) != int(wave_n):
            continue
        return record
    return None


def is_wave_pass_fresh(record: WavePassRecord, *, now: Optional[float] = None,
                       max_age_sec: int = WAVE_PASS_FRESHNESS_SEC) -> bool:
    """True iff ``record.passed_at`` is within ``max_age_sec`` of ``now``.

    Tolerant of unparseable timestamps — returns False rather than raising
    so callers can treat a malformed record as "stale, re-evaluate".
    """
    if not record or not record.passed_at:
        return False
    if now is None:
        now = time.time()
    try:
        passed_struct = time.strptime(record.passed_at, "%Y-%m-%dT%H:%M:%SZ")
        passed_epoch = float(_calendar_timegm(passed_struct))
    except (ValueError, TypeError):
        return False
    return (now - passed_epoch) <= max_age_sec


def _calendar_timegm(struct) -> int:
    """Inverse of ``time.gmtime``. Avoids the ``calendar`` import at module
    top so the rest of the module's surface stays unchanged.
    """
    import calendar
    return calendar.timegm(struct)


# ── Gate-ACK persistence (Fix D) ───────────────────────────────────────────
#
# Cristian-approved gate ACKs (e.g. SS-08) are persisted to soul memory keyed
# by ``(linear_project_id, gate_name)`` so a daemon restart inside the same
# project does not re-prompt for an ACK already given. Without this, every
# daemon bounce burns a fresh "ACK SS-08" round-trip with Cristian — observed
# 3x on 2026-04-26 across v7ab/v7ac/v7ad runs.
#
# Read on gate evaluation by ``AutonomousBuildOrchestrator._maybe_ss08_gate``;
# written after ``run_ss08_gate`` reports a successful ACK detection.


@dataclass
class GateAckRecord:
    """Persisted Cristian-approved gate ACK.

    All fields are optional except ``linear_project_id``, ``gate_name``, and
    ``acked_at``; the rest are diagnostic (which message, which user). Forward-
    compat: extra keys in stored JSON are dropped on load rather than raising.
    """

    linear_project_id: str
    gate_name: str
    acked_at: str  # ISO-8601 UTC, "Z"-suffixed
    acked_by_user_id: Optional[str] = None
    ack_message_ts: Optional[str] = None
    ack_message_text: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, blob: str) -> "GateAckRecord":
        data = json.loads(blob)
        known = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


async def record_gate_ack(
    soul_client,
    *,
    linear_project_id: str,
    gate_name: str,
    acked_by_user_id: Optional[str] = None,
    ack_message_ts: Optional[str] = None,
    ack_message_text: Optional[str] = None,
    acked_at: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Persist a gate-ACK record under the canonical gate-ack topic.

    ``acked_at`` defaults to UTC now() in the ``%Y-%m-%dT%H:%M:%SZ`` format
    used elsewhere in this module.

    Failures are logged + swallowed; the orchestrator's existing in-process
    ``state.ss08_acked`` flag is the source of truth for the running session,
    so a soul write failure only costs us cross-restart persistence.
    """
    if soul_client is None:
        logger.debug("record_gate_ack skipped: soul_client is None (dry-run?)")
        return None
    if not linear_project_id:
        logger.debug("record_gate_ack skipped: empty linear_project_id")
        return None
    if not gate_name:
        logger.debug("record_gate_ack skipped: empty gate_name")
        return None
    if acked_at is None:
        acked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record = GateAckRecord(
        linear_project_id=linear_project_id,
        gate_name=gate_name,
        acked_at=acked_at,
        acked_by_user_id=acked_by_user_id,
        ack_message_ts=ack_message_ts,
        ack_message_text=ack_message_text,
    )
    topic = gate_ack_topic_for(linear_project_id, gate_name)
    try:
        blob = record.to_json()
    except (TypeError, ValueError):
        logger.exception("failed to serialise gate-ack record")
        return None
    try:
        return await soul_client.write_memory(
            blob,
            topics=[topic, STATE_TOPIC_PREFIX, GATE_ACK_TOPIC_INFIX],
        )
    except Exception:
        logger.exception(
            "soul write_memory failed during record_gate_ack "
            "(project=%s gate=%s)", linear_project_id, gate_name,
        )
        return None


async def lookup_gate_ack(
    soul_client,
    *,
    linear_project_id: str,
    gate_name: str,
) -> Optional[GateAckRecord]:
    """Fetch the most recent gate-ACK record for ``(project, gate)``.

    Returns ``None`` if no record exists, the record is malformed, or the
    soul lookup fails. Returns the parsed ``GateAckRecord`` otherwise — the
    caller is responsible for checking ``acked_at`` freshness via
    ``is_gate_ack_fresh``.
    """
    if soul_client is None or not linear_project_id or not gate_name:
        return None
    topic = gate_ack_topic_for(linear_project_id, gate_name)
    try:
        recent = await soul_client.recent_memories(limit=5, topics=[topic])
    except Exception:
        logger.exception(
            "soul recent_memories failed during lookup_gate_ack "
            "(project=%s gate=%s)", linear_project_id, gate_name,
        )
        return None
    if isinstance(recent, dict):
        recent = recent.get("memories") or []
    if not isinstance(recent, list) or not recent:
        return None
    for mem in recent:
        content = (mem or {}).get("content") if isinstance(mem, dict) else None
        if not content:
            continue
        try:
            record = GateAckRecord.from_json(content)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning(
                "skipping malformed gate-ack record (project=%s gate=%s)",
                linear_project_id, gate_name,
            )
            continue
        # Belt-and-braces: reject records whose embedded keys disagree with
        # our query. A topic collision shouldn't happen, but cheap to check.
        if record.linear_project_id and record.linear_project_id != linear_project_id:
            logger.warning(
                "ignored gate-ack record with mismatched project id %s "
                "(wanted %s)", record.linear_project_id, linear_project_id,
            )
            continue
        if record.gate_name and record.gate_name != gate_name:
            continue
        return record
    return None


def is_gate_ack_fresh(record: GateAckRecord, *, now: Optional[float] = None,
                      max_age_sec: int = GATE_ACK_FRESHNESS_SEC) -> bool:
    """True iff ``record.acked_at`` is within ``max_age_sec`` of ``now``.

    Tolerant of unparseable timestamps — returns False rather than raising
    so callers can treat a malformed record as "stale, re-prompt".
    """
    if not record or not record.acked_at:
        return False
    if now is None:
        now = time.time()
    try:
        acked_struct = time.strptime(record.acked_at, "%Y-%m-%dT%H:%M:%SZ")
        acked_epoch = float(_calendar_timegm(acked_struct))
    except (ValueError, TypeError):
        return False
    return (now - acked_epoch) <= max_age_sec
