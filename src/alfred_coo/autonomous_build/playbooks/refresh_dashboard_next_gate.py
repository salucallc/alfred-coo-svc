"""Playbook: refresh the v7 dashboard's "Next Gate" paragraph from live state.

The v7 dashboard renders a "Next gate / critical path" panel by parsing
the first paragraph under ``## Next Gate`` in a roadmap markdown doc
(default: ``/opt/v7-dashboard/journey/roadmap_rc6_to_first_customer_2026-04-27.md``).
Without an autonomous refresher, that paragraph drifts: the doc is
typically authored once at a planning checkpoint and never touched again,
so a few days later the dashboard cheerfully announces a stale "Phase A
blocker" that has long since shipped.

This playbook fixes the dashboard-from-stale-doc anti-pattern: every
tick it reads live substrate state (daemon HEAD, last doctor activity,
current UTC timestamp), formats a one-paragraph current-state summary,
and atomically rewrites the doc's ``## Next Gate`` section. The rest of
the markdown is left intact so the per-phase progress bars, executive
summary, and ticket-coverage tables continue to render unchanged.

Idempotent: byte-stable output for unchanged inputs; re-running on the
same daemon state is a no-op (the file's mtime advances but the body is
identical so editor diff tools still see "no change"). Bounded: one
write per tick. Loud-on-error: any failure (read, parse, write) is
recorded in PlaybookResult.errors and the playbook returns a result
showing it did try, never raises.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .base import Playbook, PlaybookResult


logger = logging.getLogger(
    "alfred_coo.autonomous_build.playbooks.refresh_dashboard_next_gate"
)


DEFAULT_DOC_PATH = (
    "/opt/v7-dashboard/journey/roadmap_rc6_to_first_customer_2026-04-27.md"
)
NEXT_GATE_HEADER = "## Next Gate"


# Use ``[^\S\n]`` (whitespace minus newline) for the head clause so the
# pattern doesn't greedy-eat newlines following the header. Without this,
# each replacement run would re-include any pre-existing blank lines
# above the body in the head capture, then prepend another ``\n\n`` from
# ``new_block`` — and the body would grow by two newlines per run,
# breaking idempotency. Match exactly one trailing newline after ``Gate``.
_NEXT_GATE_BLOCK_RE = re.compile(
    r"(?P<head>^\#\#[^\S\n]*Next[^\S\n]+Gate[^\S\n]*\n)(?P<body>.*?)(?=^\#\#\s|\Z)",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)


def _read_daemon_head(repo_path: str = "/opt/alfred-coo") -> str:
    """Best-effort read of the daemon repo's short HEAD via git. Returns
    an empty string if git isn't reachable (test env, missing binary).

    Kept tiny so a missing repo / binary doesn't block the rest of the
    playbook's work."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()[:12]


async def _count_recent_doctor_ticks(
    *, mesh, since_ts: float,
) -> int:
    """Count completed alfred-doctor tasks newer than ``since_ts``.

    Used for the "last hour: N ticks" line on the rendered paragraph so
    the reader can tell at a glance whether the chain is alive. Best-
    effort: failures return 0 and a logged warning, NOT an exception."""
    try:
        completed = await mesh.list_tasks(status="completed", limit=200)
    except Exception as e:  # noqa: BLE001 — mesh failures are surveillance noise
        logger.warning(
            "refresh_dashboard_next_gate: mesh list_tasks(completed) failed: %s",
            e,
        )
        return 0
    cutoff = datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat()
    n = 0
    for t in completed:
        title = str(t.get("title") or "")
        if "[persona:alfred-doctor]" not in title:
            continue
        ts = t.get("completed_at") or ""
        if ts and ts < cutoff:
            continue
        n += 1
    return n


# Pre-dispatch structural gates that fire once at orchestrator startup
# (in ``_run_inner`` before the wave loop). Listed here so the Next Gate
# paragraph self-describes as new gates land. When a new gate ships,
# append it here — the dashboard render picks it up automatically on the
# next 5-min tick.
PRE_DISPATCH_GATES: tuple[str, ...] = (
    "APE/V hydration",
    "reference content hydration",
)


def _render_paragraph(
    *,
    head: str,
    recent_doctor_ticks: int,
    now_iso: str,
    interval_min: int,
    playbook_kinds: tuple[str, ...],
    pre_dispatch_gates: tuple[str, ...] = PRE_DISPATCH_GATES,
) -> str:
    """Compose the Next Gate paragraph from the live signals.

    Format is byte-stable for the same inputs so a chain of identical
    ticks doesn't churn the file's body bytes. The closing reminder line
    documents the auto-refresh contract so a future operator opening the
    doc directly knows not to hand-edit it.

    ``playbook_kinds`` is the live list pulled from ``DEFAULT_PLAYBOOKS``
    at render time so adding a playbook automatically updates the
    paragraph the next tick. ``pre_dispatch_gates`` covers the orchestrator
    pre-dispatch gates that fire once at startup (currently APE/V and
    reference content); update ``PRE_DISPATCH_GATES`` when a new one ships.
    """
    head_clause = f"daemon HEAD `{head}`" if head else "daemon HEAD unknown"
    activity_clause = (
        f"{recent_doctor_ticks} doctor tick"
        + ("s" if recent_doctor_ticks != 1 else "")
        + " in the last hour"
    )
    if playbook_kinds:
        playbook_list = ", ".join(playbook_kinds)
        playbook_clause = f"playbooks ({playbook_list})"
    else:
        playbook_clause = "no playbooks registered"
    if pre_dispatch_gates:
        gate_list = " + ".join(pre_dispatch_gates)
        gate_clause = f"pre-dispatch {gate_list} gates"
    else:
        gate_clause = "no pre-dispatch gates"
    return (
        f"**Substrate self-healing live (refreshed {now_iso}).** "
        f"{head_clause}; {activity_clause}; "
        f"alfred-doctor white-blood-cell + {playbook_clause} + "
        f"{gate_clause} all active. Doctor metric stream tracks errors "
        f"(real failures) and escalations (designed human-needed signals) "
        f"as distinct fields per playbook. "
        f"Phase A (rc.6 ship + 48h soak) and Phase B (MC backlog drain) "
        f"closed during the MC v1 GA marathon (2026-04-27). Live progress: "
        f"see the per-phase bars below — they're computed from Linear at "
        f"request time. "
        f"_Auto-refreshed by the alfred-doctor `refresh_dashboard_next_gate` "
        f"playbook every {interval_min} min; do not hand-edit this paragraph._"
    )


def _replace_next_gate_section(src: str, new_paragraph: str) -> str:
    """Replace the body of the ``## Next Gate`` section in ``src``.

    The header itself is preserved verbatim. The new body is the rendered
    paragraph wrapped with one blank line above and below so the
    surrounding markdown structure stays valid. Anything between the
    header and the next ``##`` (or EOF) is replaced.
    """
    new_block = f"\n\n{new_paragraph}\n\n"
    m = _NEXT_GATE_BLOCK_RE.search(src)
    if not m:
        # Header missing — append a fresh section at the end so the
        # dashboard parser starts seeing live data on the next render.
        # Pre-pend a blank line if the file doesn't already end with one
        # so we never produce ``...prev\n## Next Gate`` (no separation).
        sep = "" if src.endswith("\n\n") else "\n"
        return src.rstrip() + sep + f"\n{NEXT_GATE_HEADER}\n{new_block}"
    return src[: m.start("body")] + new_block + src[m.end("body"):]


class RefreshDashboardNextGatePlaybook(Playbook):
    """See module docstring."""

    kind = "refresh_dashboard_next_gate"
    max_actions_per_tick = 1

    def __init__(
        self,
        doc_path: str | None = None,
        repo_path: str = "/opt/alfred-coo",
        interval_min: int = 5,
    ):
        self.doc_path = doc_path or DEFAULT_DOC_PATH
        self.repo_path = repo_path
        self.interval_min = interval_min

    async def execute(
        self,
        *,
        linear_api_key: str,
        dry_run: bool,
        mesh: Any = None,
        recent_window_sec: int = 3600,
        **_extra: Any,
    ) -> PlaybookResult:
        result = PlaybookResult(kind=self.kind, dry_run=dry_run)

        path = Path(self.doc_path)
        if not path.exists():
            result.errors.append(f"doc_not_found: {self.doc_path}")
            return result

        try:
            src = path.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.exception("refresh_dashboard_next_gate: read failed")
            result.errors.append(f"read_failed: {type(e).__name__}: {str(e)[:80]}")
            return result

        head = _read_daemon_head(self.repo_path)
        recent_ticks = 0
        if mesh is not None:
            recent_ticks = await _count_recent_doctor_ticks(
                mesh=mesh,
                since_ts=time.time() - recent_window_sec,
            )

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Pull the live playbook kind list from the registry at render
        # time so the paragraph self-describes when new playbooks land.
        # Lazy import — module-level would cycle through __init__.py back
        # to this file.
        from . import DEFAULT_PLAYBOOKS  # noqa: WPS433
        playbook_kinds = tuple(pb.kind for pb in DEFAULT_PLAYBOOKS)
        new_paragraph = _render_paragraph(
            head=head,
            recent_doctor_ticks=recent_ticks,
            now_iso=now_iso,
            interval_min=self.interval_min,
            playbook_kinds=playbook_kinds,
        )

        new_src = _replace_next_gate_section(src, new_paragraph)

        # Found something to write iff the body actually changes. Same-
        # input → same-output → no-op (idempotent). Counts as a
        # candidate when a write would land regardless of dry-run.
        if new_src == src:
            return result
        result.candidates_found = 1

        if dry_run:
            result.notable.append(
                f"would refresh {self.doc_path} (HEAD={head or '?'}, "
                f"recent_ticks={recent_ticks})"
            )
            return result

        try:
            path.write_text(new_src, encoding="utf-8")
            result.actions_taken = 1
            result.notable.append(
                f"refreshed {self.doc_path} (HEAD={head or '?'}, "
                f"recent_ticks={recent_ticks})"
            )
        except PermissionError as e:
            # The dashboard's roadmap doc is typically owned by root;
            # the daemon runs as ubuntu. One-time fix on Oracle:
            #   sudo chgrp ubuntu /opt/v7-dashboard/journey/roadmap_*.md
            #   sudo chmod g+w /opt/v7-dashboard/journey/roadmap_*.md
            # Until that lands, this playbook reports a permission error
            # each tick rather than spamming a stack trace per minute.
            logger.warning(
                "refresh_dashboard_next_gate: write denied (%s); needs "
                "one-time chgrp ubuntu + chmod g+w on %s",
                e, self.doc_path,
            )
            result.errors.append(
                f"PermissionError: {self.doc_path} not writable by daemon user"
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("refresh_dashboard_next_gate: write failed")
            result.errors.append(f"write_failed: {type(e).__name__}: {str(e)[:80]}")
        return result
