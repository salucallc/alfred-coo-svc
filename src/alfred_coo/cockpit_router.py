"""Cockpit state rollup endpoint.

Exposes a single `GET /v1/cockpit/state` route that aggregates the daemon's
live orchestrator state, mesh-session heartbeats from soul-svc, and a
small `gh` snapshot of recently merged PRs into one ≤2KB JSON blob the
alfred-portal cockpit polls every 5 s.

This is deliberately one-fetch-per-poll, not a per-panel API surface — the
cockpit's single `useCockpitStream` hook reads this once per tick and slices
it into the 23 cockpit panels. Adding panel-specific endpoints belongs in
follow-up work; for now, every panel that needs live data eats from this
trough.

CORS: the alfred-portal cockpit runs in the browser on a different origin
than the daemon, so this router mounts an `Access-Control-Allow-Origin: *`
middleware. Stream B is internal tooling only — no auth tokens cross the
fetch — so wildcard CORS is fine. Tighten if/when this ever serves an
authenticated payload.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware


logger = logging.getLogger(__name__)


# ── Live orchestrator instance registry ────────────────────────────────────
#
# `main._running_orchestrators` already tracks the asyncio.Task for each
# running orchestrator, but the rollup needs the underlying
# `AutonomousBuildOrchestrator` *instance* so it can read `state.current_wave`,
# `linear_project_id`, the per-ticket status map, etc. We keep a parallel
# registry keyed by mesh task id, populated by `_spawn_long_running_handler`
# in main.py and pruned by the same done-callback chain that prunes the task
# registry. Read-only from the cockpit router's perspective.
_ORCH_INSTANCES: Dict[str, Any] = {}


def register_orchestrator(task_id: str, orch: Any) -> None:
    """Called from main.py after a successful orchestrator spawn."""
    _ORCH_INSTANCES[task_id] = orch


def deregister_orchestrator(task_id: str) -> None:
    """Called from main.py's done-callback when the orchestrator finishes."""
    _ORCH_INSTANCES.pop(task_id, None)


def list_active_orchestrators() -> List[Dict[str, Any]]:
    """Snapshot the live orchestrators into the cockpit's expected shape.

    All fields are best-effort — if the orchestrator hasn't yet parsed its
    kickoff payload (e.g. very early in `run()`), some attrs may be empty
    or zero. The cockpit accepts that and renders empty-state.
    """
    out: List[Dict[str, Any]] = []
    for task_id, orch in list(_ORCH_INSTANCES.items()):
        try:
            kickoff_label = (orch.task.get("title") or "").strip()
            state = getattr(orch, "state", None)
            graph = getattr(orch, "graph", None)
            ticket_status = (
                dict(state.ticket_status) if state and state.ticket_status else {}
            )
            tickets_total = (
                len(graph.tickets) if graph and getattr(graph, "tickets", None) else 0
            )
            # Count tickets in any "done-ish" terminal state. The orchestrator
            # uses string status values from `TicketStatus` — we count the
            # canonical green-merge states + the explicit DONE alias.
            done_states = {"DONE", "MERGED_GREEN", "MERGED", "PR_MERGED"}
            tickets_done = sum(
                1 for s in ticket_status.values() if str(s).upper() in done_states
            )
            in_flight_states = {
                "DISPATCHED",
                "REVIEWING",
                "AWAITING_REVIEW",
                "PR_OPEN",
            }
            in_flight = sum(
                1 for s in ticket_status.values() if str(s).upper() in in_flight_states
            )
            ready_states = {"READY", "QUEUED"}
            ready = sum(
                1 for s in ticket_status.values() if str(s).upper() in ready_states
            )
            spend_usd = float(getattr(state, "cumulative_spend_usd", 0.0) or 0.0)
            out.append(
                {
                    "task_id": task_id,
                    "kickoff_label": kickoff_label,
                    "linear_project_id": str(getattr(orch, "linear_project_id", "") or ""),
                    "current_wave": int(getattr(state, "current_wave", 0) or 0)
                    if state
                    else 0,
                    "tickets_done": tickets_done,
                    "tickets_total": tickets_total,
                    "in_flight": in_flight,
                    "ready": ready,
                    "spend_usd": round(spend_usd, 2),
                }
            )
        except Exception:  # pragma: no cover — defensive; skip bad rows
            logger.exception("failed to snapshot orchestrator %s", task_id)
    return out


# ── soul-svc mesh sessions (heartbeats) ────────────────────────────────────


async def _fetch_mesh_sessions(
    base_url: str, api_key: str, timeout: float = 3.0
) -> List[Dict[str, Any]]:
    """Pull active mesh sessions from soul-svc.

    Returns a list of `{node_id, harness, last_heartbeat}` rows for the
    sessions soul-svc considers `status == "active"`. Errors are logged
    and swallowed — the cockpit must render even when soul-svc is down.
    """
    url = f"{base_url.rstrip('/')}/v1/mesh/sessions"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("mesh sessions fetch failed: %s", e)
        return []
    sessions = data.get("sessions", []) if isinstance(data, dict) else []
    out: List[Dict[str, Any]] = []
    for s in sessions:
        if s.get("status") != "active":
            continue
        out.append(
            {
                "node_id": s.get("node_id") or "",
                "harness": s.get("harness") or "",
                "session_id": s.get("session_id") or "",
                "last_heartbeat": s.get("last_heartbeat") or "",
            }
        )
    return out


# ── Recent merges via `gh` ─────────────────────────────────────────────────
#
# `gh pr list --state merged` over the daemon's primary repo. We keep this
# repo-scoped to alfred-coo-svc itself — the cockpit's "recent merges" panel
# is the daemon's own ship rate; mesh-wide PR aggregation is a follow-up
# panel and a different endpoint.

_GH_REPO = os.environ.get("COCKPIT_GH_REPO", "salucallc/alfred-coo-svc")
_GH_LIMIT = int(os.environ.get("COCKPIT_GH_LIMIT", "10"))
_GH_CACHE_TTL_SEC = int(os.environ.get("COCKPIT_GH_CACHE_TTL_SEC", "30"))

_recent_merges_cache: Dict[str, Any] = {"ts": 0.0, "data": []}


async def _fetch_recent_merges() -> List[Dict[str, Any]]:
    """Cached `gh pr list` shell-out for the rollup's recent_merges block.

    Cached per `_GH_CACHE_TTL_SEC` to avoid spawning a subprocess on every
    5 s poll — `gh` startup is ~300 ms and the daemon would burn CPU for
    no reason. The cache is process-local; a daemon restart re-warms it.
    """
    now = time.time()
    if now - _recent_merges_cache["ts"] < _GH_CACHE_TTL_SEC and _recent_merges_cache["data"]:
        return _recent_merges_cache["data"]  # type: ignore[return-value]
    if not shutil.which("gh"):
        logger.debug("gh CLI not on PATH; skipping recent_merges")
        return []
    cmd = [
        "gh",
        "pr",
        "list",
        "--repo",
        _GH_REPO,
        "--state",
        "merged",
        "--limit",
        str(_GH_LIMIT),
        "--json",
        "number,title,mergedAt",
    ]
    try:
        # Run in a thread so the asyncio loop doesn't block on subprocess.
        proc = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, timeout=10
        )
        if proc.returncode != 0:
            logger.warning("gh pr list failed: %s", proc.stderr.strip()[:200])
            return _recent_merges_cache["data"]  # type: ignore[return-value]
        rows = json.loads(proc.stdout) if proc.stdout.strip() else []
    except Exception as e:
        logger.warning("recent_merges shell-out failed: %s", e)
        return _recent_merges_cache["data"]  # type: ignore[return-value]
    repo_short = _GH_REPO.split("/")[-1]
    merges = [
        {
            "repo": repo_short,
            "pr_number": int(r.get("number", 0)),
            "title": (r.get("title") or "")[:120],
            "merged_at": r.get("mergedAt") or "",
        }
        for r in rows
    ]
    _recent_merges_cache["ts"] = now
    _recent_merges_cache["data"] = merges
    return merges


# ── Public router ──────────────────────────────────────────────────────────


def make_cockpit_router(
    *,
    soul_api_url: str,
    soul_api_key: str,
    halt_state_fn: Optional[Any] = None,
    agent_count: int = 345,
) -> APIRouter:
    """Build the cockpit router bound to live config.

    `halt_state_fn` is an optional zero-arg callable returning the current
    halt state string ("dormant" / "active-full" / "active-acked"); if None
    the rollup reports "dormant". The daemon doesn't yet have a halt source
    — the orchestrator-level kill switch is per-orchestrator, not global —
    so `dormant` is the correct default until a halt registry lands.

    `agent_count` is the canonical fleet size; sourced from the
    Saluca twin_tenants registry per memory `reference_agent_fleet_canonical`
    (345 as of 2026-04-15). Surfaced as a bare integer in the rollup
    because the cockpit MeshState panel renders it directly.
    """
    router = APIRouter()

    @router.get("/v1/cockpit/state")
    async def cockpit_state() -> Dict[str, Any]:
        sessions = await _fetch_mesh_sessions(soul_api_url, soul_api_key)
        merges = await _fetch_recent_merges()
        halt = "dormant"
        if halt_state_fn is not None:
            try:
                halt = str(halt_state_fn() or "dormant")
            except Exception:
                halt = "dormant"
        return {
            "halt_state": halt,
            "active_orchestrators": list_active_orchestrators(),
            "mesh": {
                "active_nodes": sessions,
                "agent_count": agent_count,
            },
            "recent_merges": merges,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    return router


def attach_cockpit(
    app: FastAPI,
    *,
    soul_api_url: str,
    soul_api_key: str,
    halt_state_fn: Optional[Any] = None,
    agent_count: int = 345,
    cors_origins: Optional[List[str]] = None,
) -> None:
    """Mount the cockpit router + CORS onto an existing FastAPI app.

    Called from `main.py` after `health.make_app()` so the same uvicorn
    instance serves `/healthz` and `/v1/cockpit/state`.
    """
    if cors_origins is None:
        cors_origins = ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET"],
        allow_headers=["*"],
        allow_credentials=False,
    )
    router = make_cockpit_router(
        soul_api_url=soul_api_url,
        soul_api_key=soul_api_key,
        halt_state_fn=halt_state_fn,
        agent_count=agent_count,
    )
    app.include_router(router)
