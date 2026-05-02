"""Playbook: restart stalled autonomous-build orchestrator chains.

Today's failure mode (caught while investigating "why are there no
builder dispatches in Grafana"): an active project's autonomous-build-a
orchestrator chain crashed, no one queued a fresh kickoff, and the
project went silent for hours. Linear backlog grew while the chain was
dead.

This playbook closes the loop: every doctor tick scans the active MC v1
GA projects and, for each project that has Backlog tickets but no live
autonomous-build orchestrator chain, queues a fresh kickoff. Bounded by
``DEFAULT_ATTEMPT_BUDGET`` restart attempts per project per
``DEFAULT_ATTEMPT_WINDOW_SEC`` window; once the budget is exhausted the
playbook stops auto-restarting and escalates to the operator (the error
appears in the Slack digest and the doctor's metric stream so Cristian
sees that human attention is needed without auto-loop noise).

Why the budget matters: today's chain died because ~4 specific tickets
were genuinely unbuildable. Without an attempt budget, an auto-restart
playbook would just relaunch the chain into the same crash pattern
infinitely. The budget says "after N tries, this isn't going to fix
itself — surface to a human."

Mechanics:
* "Chain alive" = at least one mesh task with title matching
  ``[persona:autonomous-build-a]`` AND status="claimed" AND
  ``linear_project_id`` (parsed from description JSON) matching the
  project AND heartbeat within ``DEFAULT_HEARTBEAT_FRESHNESS_SEC``.
* Restart attempts persisted to JSON at
  ``/var/lib/alfred-coo/restart_history.json`` so daemon restarts don't
  reset the budget.
* Idempotent: if the playbook just queued a kickoff and the daemon
  hasn't claimed it yet, a subsequent tick within seconds finds the
  pending kickoff (still un-claimed) — wait, actually we only check
  ``claimed``, not ``pending``. Need to also check pending so we don't
  double-queue. See ``_chain_state``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

from .base import Playbook, PlaybookResult


logger = logging.getLogger("alfred_coo.autonomous_build.playbooks.restart_stalled_chains")


# Active MC v1 GA project IDs. Keep in sync with hydrate_apev.py's list.
DEFAULT_ACTIVE_PROJECTS: dict[str, str] = {
    "Cockpit-UX":   "5a014234-df36-47a0-9abb-eac093e27539",
    "MSSP-Ext":     "39e340a8-26d2-4439-8582-caf94a263c7e",
    "MSSP-Fed":     "a9d93b23-96b4-4a77-be18-b709f72fa3ce",
    "Agent-Ingest": "9db00c4f-17a4-4b7a-8cd8-ea62f45d55b8",
}

DEFAULT_ATTEMPT_BUDGET = 3
DEFAULT_ATTEMPT_WINDOW_SEC = 3600       # 1h sliding window
DEFAULT_HEARTBEAT_FRESHNESS_SEC = 1800  # chain considered alive if heartbeat <30 min
# Substrate task #87 (2026-05-02): minimum gap between successive restart
# attempts for the same project. Without this, the doctor's 5-minute
# surveillance tick can fire 3 budget-permitted restarts in 15 minutes when
# the chain is genuinely dead — burning the entire 1h budget while the
# operator hasn't had time to look. 10-minute cooldown spaces attempts so
# Cristian sees the first stalled-chain Slack message before the playbook
# decides the chain is irrecoverable.
DEFAULT_ATTEMPT_COOLDOWN_SEC = 600
DEFAULT_HISTORY_PATH = "/var/lib/alfred-coo/restart_history.json"


def _load_history(path: str) -> dict[str, list[float]]:
    """Load persisted restart history from JSON file. Empty dict on any
    error (missing file, parse failure) — never raises."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.warning(
            "restart_stalled_chains: history load failed (path=%s); "
            "starting fresh", path,
        )
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[float]] = {}
    for k, v in data.items():
        if not isinstance(v, list):
            continue
        try:
            out[str(k)] = [float(x) for x in v]
        except (TypeError, ValueError):
            continue
    return out


def _save_history(
    path: str,
    history: dict[str, list[float]],
    *,
    now: float,
    window_sec: int = DEFAULT_ATTEMPT_WINDOW_SEC,
) -> None:
    """Persist history, pruning entries older than ``window_sec``. Best-
    effort: failure is logged and swallowed."""
    cutoff = now - window_sec
    pruned = {
        pid: sorted(t for t in attempts if t >= cutoff)
        for pid, attempts in history.items()
    }
    pruned = {pid: attempts for pid, attempts in pruned.items() if attempts}
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(pruned, separators=(",", ":")),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "restart_stalled_chains: history save failed (path=%s)", path,
        )


def _recent_attempts(
    history: dict[str, list[float]],
    project_id: str,
    *,
    now: float,
    window_sec: int = DEFAULT_ATTEMPT_WINDOW_SEC,
) -> list[float]:
    """Return restart-attempt timestamps for ``project_id`` within the
    last ``window_sec``. Pure function over the history dict."""
    cutoff = now - window_sec
    return [t for t in history.get(project_id, []) if t >= cutoff]


async def _count_backlog_tickets(
    client: httpx.AsyncClient,
    *,
    linear_api_key: str,
    project_id: str,
) -> int:
    """Count Linear tickets in ``project_id`` with state name "Backlog"
    (the orchestrator's dispatch source). Errors propagate to the
    caller — the playbook treats project-scan failures as "skip this
    project this tick"."""
    q = """query Q($pid: String!) {
        project(id: $pid) {
            issues(first: 200) {
                nodes { state { name } }
            }
        }
    }"""
    resp = await client.post(
        "https://api.linear.app/graphql",
        headers={"Authorization": linear_api_key, "Content-Type": "application/json"},
        content=json.dumps({"query": q, "variables": {"pid": project_id}}).encode(),
    )
    data = resp.json()
    nodes = (
        (data.get("data") or {})
        .get("project", {})
        .get("issues", {})
        .get("nodes", [])
        or []
    )
    return sum(
        1 for n in nodes
        if ((n.get("state") or {}).get("name") or "").strip() == "Backlog"
    )


async def _chain_alive_for_project(
    mesh,
    *,
    project_id: str,
    now: float,
    freshness_sec: int = DEFAULT_HEARTBEAT_FRESHNESS_SEC,
) -> bool:
    """True iff there's a live autonomous-build orchestrator for the
    given project.

    "Live" = a mesh task with title prefixed ``[persona:autonomous-build-a]``
    AND ``linear_project_id`` (parsed from the description JSON) matching
    AND status in {pending, claimed} AND, for claimed tasks, last
    heartbeat within ``freshness_sec``. Pending counts as alive too —
    the daemon will pick it up on next poll.
    """
    for status in ("pending", "claimed"):
        try:
            tasks = await mesh.list_tasks(status=status, limit=200)
        except Exception:  # noqa: BLE001
            logger.exception(
                "restart_stalled_chains: mesh.list_tasks(%s) failed", status,
            )
            continue
        for t in tasks:
            title = str(t.get("title") or "")
            if not title.startswith("[persona:autonomous-build-a]"):
                continue
            desc = t.get("description") or ""
            try:
                payload = json.loads(desc) if isinstance(desc, str) else (desc or {})
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if str(payload.get("linear_project_id") or "") != project_id:
                continue
            if status == "pending":
                return True
            # Claimed — check heartbeat freshness.
            hb = t.get("heartbeat_at") or t.get("claimed_at") or ""
            if not hb:
                # No heartbeat data — be conservative, treat as alive
                # (better to skip a restart than double-up).
                return True
            try:
                from datetime import datetime
                hb_ts = datetime.fromisoformat(hb.replace("Z", "+00:00")).timestamp()
            except Exception:  # noqa: BLE001
                return True
            if (now - hb_ts) <= freshness_sec:
                return True
    return False


async def _queue_kickoff(
    mesh,
    *,
    project_id: str,
    project_name: str,
    session_id: str,
) -> str:
    """Queue a fresh autonomous-build-a kickoff for the project.

    Payload is the minimum viable shape — orchestrator fills the rest
    from defaults. wave_retry_budget=2 gives the new chain three
    attempts per wave before terminal failure.
    """
    payload = {
        "linear_project_id": project_id,
        "wave_retry_budget": 2,
        "auto_restarted_by": "alfred-doctor.restart_stalled_chains",
    }
    title = (
        f"[persona:autonomous-build-a] {project_name} (auto-restart)"
    )
    resp = await mesh.create_task(
        title=title,
        description=json.dumps(payload),
        from_session_id=session_id,
    )
    if not isinstance(resp, dict):
        raise RuntimeError(
            f"create_task returned non-dict: {type(resp).__name__}"
        )
    task_id = resp.get("id")
    if not task_id:
        raise RuntimeError(f"create_task returned no id: {str(resp)[:200]}")
    return str(task_id)


class RestartStalledChainsPlaybook(Playbook):
    """See module docstring."""

    kind = "restart_stalled_chains"
    max_actions_per_tick = 4   # one per active project

    def __init__(
        self,
        *,
        projects: dict[str, str] | None = None,
        history_path: str | None = None,
        attempt_budget: int = DEFAULT_ATTEMPT_BUDGET,
        attempt_window_sec: int = DEFAULT_ATTEMPT_WINDOW_SEC,
        attempt_cooldown_sec: int = DEFAULT_ATTEMPT_COOLDOWN_SEC,
        heartbeat_freshness_sec: int = DEFAULT_HEARTBEAT_FRESHNESS_SEC,
        from_session_id: str = "alfred-coo",
    ):
        self.projects = (
            projects if projects is not None else DEFAULT_ACTIVE_PROJECTS
        )
        self.history_path = history_path or DEFAULT_HISTORY_PATH
        self.attempt_budget = attempt_budget
        self.attempt_window_sec = attempt_window_sec
        self.attempt_cooldown_sec = attempt_cooldown_sec
        self.heartbeat_freshness_sec = heartbeat_freshness_sec
        self.from_session_id = from_session_id

    async def execute(
        self,
        *,
        linear_api_key: str,
        dry_run: bool,
        mesh: Any = None,
        **_extra: Any,
    ) -> PlaybookResult:
        result = PlaybookResult(kind=self.kind, dry_run=dry_run)
        if mesh is None:
            result.errors.append("mesh kwarg missing")
            return result
        if not linear_api_key:
            result.errors.append("linear_api_key missing")
            return result

        history = _load_history(self.history_path)
        now = time.time()

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                for name, pid in self.projects.items():
                    await self._handle_project(
                        client=client,
                        mesh=mesh,
                        history=history,
                        now=now,
                        name=name,
                        pid=pid,
                        linear_api_key=linear_api_key,
                        dry_run=dry_run,
                        result=result,
                    )
        except Exception as e:  # noqa: BLE001
            logger.exception("restart_stalled_chains: client lifecycle failed")
            result.errors.append(
                f"client_lifecycle_failed: {type(e).__name__}"
            )

        # Persist (pruned) history regardless of outcome so the budget
        # window slides forward.
        _save_history(
            self.history_path, history,
            now=now, window_sec=self.attempt_window_sec,
        )
        return result

    async def _handle_project(
        self,
        *,
        client: httpx.AsyncClient,
        mesh,
        history: dict[str, list[float]],
        now: float,
        name: str,
        pid: str,
        linear_api_key: str,
        dry_run: bool,
        result: PlaybookResult,
    ) -> None:
        # 1. Linear backlog count.
        try:
            backlog = await _count_backlog_tickets(
                client, linear_api_key=linear_api_key, project_id=pid,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("restart_stalled_chains: backlog query failed for %s", name)
            result.errors.append(
                f"{name}: backlog_query_failed: {type(e).__name__}"
            )
            return
        if backlog == 0:
            # Project is done — no action needed.
            return

        # 2. Chain alive?
        try:
            alive = await _chain_alive_for_project(
                mesh, project_id=pid, now=now,
                freshness_sec=self.heartbeat_freshness_sec,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "restart_stalled_chains: mesh check failed for %s", name,
            )
            result.errors.append(
                f"{name}: mesh_check_failed: {type(e).__name__}"
            )
            return
        if alive:
            result.actions_skipped += 1
            return

        # 3. Restart-budget check.
        recent = _recent_attempts(
            history, pid, now=now, window_sec=self.attempt_window_sec,
        )
        if len(recent) >= self.attempt_budget:
            # Escalate, don't auto-loop. ``escalations`` (not ``errors``) so
            # Phase 3b deviation detection treats this as the designed signal
            # it is, not as a playbook regression.
            window_min = self.attempt_window_sec // 60
            ticket_word = "ticket" if backlog == 1 else "tickets"
            result.escalations.append(
                f"{name}: stalled with {backlog} backlog {ticket_word}; "
                f"restart budget exhausted "
                f"({len(recent)} attempts in last {window_min}min)"
            )
            return

        # 3b. Per-project cooldown check (substrate task #87, 2026-05-02).
        # Even with budget remaining, refuse to fire a fresh kickoff if the
        # last attempt for THIS project was less than ``attempt_cooldown_sec``
        # ago. Doctor surveillance ticks at 5-minute intervals; without a
        # cooldown the playbook can burn the full 3-attempt budget in 15
        # minutes when a chain is genuinely dead, leaving no headroom for
        # operator intervention. 10-minute cooldown spaces attempts so the
        # first failed-restart Slack notification reaches Cristian before
        # the playbook escalates to "irrecoverable".
        if recent and self.attempt_cooldown_sec > 0:
            last_attempt = max(recent)
            since_last = now - last_attempt
            if since_last < self.attempt_cooldown_sec:
                cooldown_min = self.attempt_cooldown_sec // 60
                wait_sec = int(self.attempt_cooldown_sec - since_last)
                result.actions_skipped += 1
                result.notable.append(
                    f"{name}: cooldown active "
                    f"({int(since_last)}s since last attempt; "
                    f"min {cooldown_min}min, wait {wait_sec}s)"
                )
                return

        # 4. Queue fresh kickoff.
        result.candidates_found += 1
        if dry_run:
            result.notable.append(
                f"would restart {name} ({backlog} backlog, "
                f"{len(recent)} prior attempts in window)"
            )
            return

        try:
            task_id = await _queue_kickoff(
                mesh,
                project_id=pid,
                project_name=name,
                session_id=self.from_session_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "restart_stalled_chains: kickoff queue failed for %s", name,
            )
            result.errors.append(
                f"{name}: kickoff_queue_failed: {type(e).__name__}"
            )
            return

        result.actions_taken += 1
        result.notable.append(
            f"restarted {name} -> {task_id[:8]} ({backlog} backlog)"
        )
        history.setdefault(pid, []).append(now)
