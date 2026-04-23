"""Orchestrator state container + soul-memory checkpoint/restore.

The orchestrator persists a structured JSON snapshot to soul memory on
every loop iteration. On daemon restart the orchestrator reads the most
recent snapshot back and resumes where it left off — wave index, per-
ticket status map, cumulative spend, and flags like `ss08_acked`.

Plan F §1 R2: state lives in soul memory, not a bespoke Supabase table.
Topic convention: `autonomous_build:<kickoff_task_id>:state`.
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


def state_topic_for(kickoff_task_id: str) -> str:
    return f"{STATE_TOPIC_PREFIX}:{kickoff_task_id}:{STATE_TOPIC_SUFFIX}"


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
