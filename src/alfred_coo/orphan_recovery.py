"""Substrate task #80 (2026-05-02): boot-time orphan-claimed-task recovery.

Extracted from `main.py` so tests can import without dragging FastAPI +
uvicorn + httpx through the dependency chain (the daemon module imports
the health server at module load time, which makes pytest collection
brittle on environments where any optional FastAPI dep is missing).

A daemon restart leaves any in-flight task claimed by ``alfred-coo``
session_id but with no living process to advance it. Pre-fix, those
tasks froze the chain until a manual mesh API patch on each one. This
helper scans claimed tasks at startup and marks every orphan as failed
so the wave-gate / doctor pipelines can resume.
"""

from __future__ import annotations

import logging
import time
from typing import Any


logger = logging.getLogger("alfred_coo.orphan_recovery")


async def recover_orphaned_tasks(mesh: Any, *, session_id: str) -> int:
    """Mark every mesh task still ``claimed`` by ``session_id`` as failed.

    A daemon restart (intentional deploy or systemd crash) leaves any
    in-flight task claim assigned to this session_id but with no living
    process. The 2026-05-02 session cleaned 8 such orphans by hand
    across three restart cycles before this automation landed.

    Logic: pull ``claimed`` tasks (limit=100 — far more than realistic
    orphan count), filter for ``assigned_session_id == session_id``,
    PATCH each to ``status=failed`` with ``reason="orphaned_by_daemon_restart"``
    so log parsers + the doctor's ``escalations`` field can disambiguate
    it from real builder failures. Returns the orphan count for log /
    metric emission.

    Idempotent: clean shutdown (no orphans) → single GET, no completions.
    Best-effort: any RPC failure is logged and swallowed so daemon
    startup is never blocked.
    """
    try:
        claimed = await mesh.list_tasks(status="claimed", limit=100)
    except Exception:
        logger.exception(
            "[orphan-recovery] mesh.list_tasks failed; skipping recovery this boot"
        )
        return 0

    recovered = 0
    for task in claimed or []:
        if not isinstance(task, dict):
            continue
        if str(task.get("assigned_session_id") or "") != session_id:
            continue
        task_id = task.get("id")
        title = task.get("title") or ""
        if not task_id:
            continue
        try:
            await mesh.complete(
                task_id=task_id,
                session_id=session_id,
                status="failed",
                result={
                    "reason": "orphaned_by_daemon_restart",
                    "recovered_at": time.time(),
                    "title_excerpt": title[:120],
                },
            )
        except Exception:
            logger.exception(
                "[orphan-recovery] failed to complete orphan task %s",
                task_id,
            )
            continue
        recovered += 1
        logger.info(
            "[orphan-recovery] marked orphan failed",
            extra={"task_id": task_id, "title": title[:120]},
        )
    if recovered:
        logger.info(
            "[orphan-recovery] recovered %d orphaned task(s) from prior daemon run",
            recovered,
        )
    return recovered
