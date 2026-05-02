"""Alfred-doctor: surveillance loop for substrate health (Phase 1).

The doctor is the "white blood cell" persona — a long-running
orchestrator that periodically scans the mesh, daemon journal, and
Linear for fleet-failure patterns and posts a digest to #batcave.

Phase 1 (this module): surveillance + Slack reporting only. No
autonomous actions are taken yet. After confidence builds, Phase 2 will
add bounded action playbooks (orphan-reset, false-positive
grounding-gap cancellation, Linear APE/V hydration).

The doctor self-perpetuates: each kickoff runs one scan, posts the
digest, then queues a fresh doctor kickoff before exiting cleanly.
Daemon restarts heal naturally because the next kickoff is already
sitting pending in the mesh queue.

Wire-up:
* Persona ``alfred-doctor`` in ``persona.py`` declares
  ``handler="AlfredDoctorOrchestrator"``.
* This module is registered in ``main.py::_HANDLER_MODULES`` so
  ``_resolve_handler`` finds the class.
* Fire the first kickoff via ``mesh_task_create`` with title prefixed
  ``[persona:alfred-doctor]`` and payload (JSON):
  ``{"interval_seconds": 300, "slack_channel": "C0ASAKFTR1C"}``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .doctor_metrics import (
    DEFAULT_BASELINE_WINDOW_SEC,
    build_snapshot_from_doctor,
    compute_baseline,
    format_baseline_summary,
    load_recent_snapshots,
    metrics_path_from_env,
    record_snapshot,
)
from .playbooks import DEFAULT_PLAYBOOKS, PlaybookResult


logger = logging.getLogger("alfred_coo.autonomous_build.doctor")


DEFAULT_INTERVAL_SECONDS = 300            # 5 min between scans
DEFAULT_SLACK_CHANNEL = "C0ASAKFTR1C"     # #batcave
DEFAULT_JOURNAL_LOOKBACK_SECONDS = 600    # one extra interval of overlap
DEFAULT_MAX_TICKS_PER_KICKOFF = 1         # one scan per kickoff, then queue next


@dataclass
class ScanFinding:
    """A single observation extracted from one of the scan sources."""
    kind: str
    detail: str
    count: int = 1


@dataclass
class ScanReport:
    """Aggregate of one scan tick. Fed into ``format_slack_message``."""
    started_at: float
    finished_at: float = 0.0
    findings: list[ScanFinding] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    grounding_gaps: list[str] = field(default_factory=list)  # ticket idents
    notable_lines: list[str] = field(default_factory=list)   # journal samples

    def add(self, kind: str, detail: str = "", count: int = 1) -> None:
        self.findings.append(ScanFinding(kind=kind, detail=detail, count=count))
        self.counters[kind] = self.counters.get(kind, 0) + count


def _parse_interval_seconds(payload: dict) -> int:
    raw = payload.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
    try:
        secs = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_SECONDS
    # Clamp to [60, 3600] so a misconfigured payload can't burn budget.
    return max(60, min(3600, secs))


def _parse_slack_channel(payload: dict) -> str:
    raw = payload.get("slack_channel") or DEFAULT_SLACK_CHANNEL
    return str(raw).strip() or DEFAULT_SLACK_CHANNEL


_GROUNDING_GAP_RE = re.compile(r"grounding gap:?\s*(SAL-\d+)?", re.IGNORECASE)
_SAL_TICKET_RE = re.compile(r"SAL-\d+")


def _extract_grounding_gap_idents(result: dict) -> list[str]:
    """Find any Linear ticket idents created via linear_create_issue with a
    grounding-gap-style title in this result envelope."""
    out: list[str] = []
    summary = str(result.get("summary") or "")
    if "grounding gap" in summary.lower() or "Escalated" in summary:
        out.extend(_SAL_TICKET_RE.findall(summary))
    for call in result.get("tool_calls") or []:
        if call.get("name") != "linear_create_issue":
            continue
        try:
            args = call.get("arguments") or "{}"
            if isinstance(args, str):
                args = json.loads(args)
        except (TypeError, ValueError):
            continue
        title = str(args.get("title") or "")
        if "grounding gap" in title.lower():
            res = call.get("result") or "{}"
            if isinstance(res, str):
                try:
                    res = json.loads(res)
                except (TypeError, ValueError):
                    res = {}
            ident = res.get("identifier") or ""
            if ident:
                out.append(str(ident))
    # Dedupe preserving order.
    seen: set[str] = set()
    return [t for t in out if not (t in seen or seen.add(t))]


def _classify_failure_mode(result: dict, status: str) -> str:
    """Return a short string tagging this task's failure mode."""
    if result.get("silent_with_tools") is True:
        return "silent_with_tools"
    summary = str(result.get("summary") or "").lower()
    if "grounding gap" in summary or "escalated" in summary:
        return "grounding_gap_escalation"
    if status == "failed":
        if "timeout" in summary or "hard-timeout" in summary:
            return "hard_timeout"
        return "other_failed"
    return "other_completed"


async def scan_mesh_recent_tasks(
    mesh,
    *,
    since_ts: float,
    report: ScanReport,
) -> None:
    """Pull recent failed + completed tasks from mesh; classify each."""
    try:
        failed = await mesh.list_tasks(status="failed", limit=50)
    except Exception as e:
        logger.warning("doctor: mesh list_tasks(failed) failed: %s", e)
        failed = []
    try:
        completed = await mesh.list_tasks(status="completed", limit=50)
    except Exception as e:
        logger.warning("doctor: mesh list_tasks(completed) failed: %s", e)
        completed = []

    cutoff = datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat()
    for tasks, status_label in ((failed, "failed"), (completed, "completed")):
        for t in tasks:
            ts = t.get("completed_at") or t.get("claimed_at") or ""
            if ts and ts < cutoff:
                continue
            result = t.get("result") or {}
            if not isinstance(result, dict):
                result = {}
            mode = _classify_failure_mode(result, status_label)
            ident_match = _SAL_TICKET_RE.search(str(t.get("title") or ""))
            ident = ident_match.group(0) if ident_match else "(no-SAL)"
            report.add(mode, detail=ident)
            for gg in _extract_grounding_gap_idents(result):
                report.grounding_gaps.append(gg)


def scan_journal_via_subprocess(
    *,
    lookback_seconds: int,
    report: ScanReport,
    binary: str = "journalctl",
    unit: str = "alfred-coo",
) -> None:
    """Tail the daemon journal and count health-relevant lines.

    Best-effort — if journalctl isn't present (test env, non-systemd
    host), record a counter and continue. Doctor never crashes on
    surveillance failure.
    """
    try:
        proc = subprocess.run(
            [
                binary,
                "-u",
                unit,
                "--since",
                f"{lookback_seconds} seconds ago",
                "--no-pager",
                "--output=cat",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        report.add("journal_unavailable", detail=str(e)[:80])
        return

    out = proc.stdout or ""
    for pattern, kind in (
        ("silent_with_tools detected", "journal_silent_with_tools"),
        ("[wave-retry] queued fresh kickoff", "journal_wave_retry_fired"),
        ("[wave-retry] scheduling fresh kickoff failed", "journal_wave_retry_broken"),
        ("decision=failed_below_threshold", "journal_wave_gate_failed"),
        ("builder hard-timeout:", "journal_hard_timeout"),
        ("[infra_retry]", "journal_infra_retry"),
        ("autonomous_build orchestrator crashed", "journal_orchestrator_crash"),
    ):
        count = out.count(pattern)
        if count:
            report.add(kind, count=count)
            # Sample one matching line for the slack digest.
            for line in out.splitlines():
                if pattern in line:
                    report.notable_lines.append(line[:240])
                    break


async def scan_linear_grounding_gaps(
    *,
    linear_api_key: str,
    since_ts: float,
    report: ScanReport,
) -> None:
    """Linear search for grounding-gap tickets created since the last tick."""
    if not linear_api_key:
        report.add("linear_skipped_no_key")
        return
    cutoff_iso = datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat()
    query = """query Q($cutoff: DateTimeOrDuration!) {
        issues(filter: {createdAt: {gte: $cutoff},
                        title: {containsIgnoreCase: "grounding gap"}},
               first: 30, orderBy: createdAt) {
            nodes { identifier createdAt }
        }
    }"""
    payload = json.dumps({"query": query, "variables": {"cutoff": cutoff_iso}}).encode()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.linear.app/graphql",
                headers={
                    "Authorization": linear_api_key,
                    "Content-Type": "application/json",
                },
                content=payload,
            )
            data = resp.json()
    except Exception as e:
        report.add("linear_query_failed", detail=str(e)[:80])
        return

    nodes = (
        (data.get("data") or {})
        .get("issues", {})
        .get("nodes", [])
        or []
    )
    for n in nodes:
        ident = n.get("identifier") or ""
        if ident:
            report.grounding_gaps.append(ident)
            report.add("linear_grounding_gap_filed", detail=ident)


def format_slack_message(
    report: ScanReport,
    daemon_head: str = "",
    playbook_results: list[PlaybookResult] | None = None,
    baseline_line: str = "",
) -> str:
    """Render a tight Slack digest of a scan report.

    ``playbook_results`` are appended after the scan section. When all
    playbooks are silent and the surveillance scan finds nothing, the
    digest collapses to ``Substrate quiet.`` so the channel doesn't get
    noised up by every empty tick.

    ``baseline_line`` is a Phase 3a optional one-liner (already
    indented two spaces) showing baseline-soak progress or summary
    percentiles. Always rendered when supplied so the operator can see
    the metric stream is alive even on otherwise-quiet ticks.
    """
    duration_s = max(0.0, report.finished_at - report.started_at)
    started_iso = datetime.fromtimestamp(report.started_at, tz=timezone.utc).strftime("%H:%M:%SZ")
    head_line = (
        f"[doctor scan {started_iso} · {duration_s:.1f}s"
        f"{' · ' + daemon_head if daemon_head else ''}]"
    )

    playbook_results = playbook_results or []
    playbook_lines: list[str] = []
    for pr in playbook_results:
        playbook_lines.extend(pr.render_digest_lines())

    surveillance_silent = not report.counters and not report.grounding_gaps
    if surveillance_silent and not playbook_lines:
        # Even on a quiet tick we want to show the baseline line if we
        # have one, so the metric stream is visibly alive. Slack-tight
        # form: head + quiet line + (optional) baseline line.
        body = "No failure-class signals in window. Substrate quiet."
        if baseline_line:
            return f"{head_line}\n{body}\n{baseline_line}"
        return f"{head_line}\n{body}"

    lines = [head_line]
    interesting_kinds = (
        "silent_with_tools", "grounding_gap_escalation", "hard_timeout",
        "journal_silent_with_tools", "journal_wave_retry_fired",
        "journal_wave_gate_failed", "journal_hard_timeout",
        "journal_infra_retry", "journal_orchestrator_crash",
        "linear_grounding_gap_filed",
    )
    for kind in interesting_kinds:
        n = report.counters.get(kind, 0)
        if n:
            lines.append(f"  {kind}: {n}")
    if report.grounding_gaps:
        unique = list(dict.fromkeys(report.grounding_gaps))[:8]
        lines.append(f"  grounding-gap tickets seen: {', '.join(unique)}")
    if report.notable_lines:
        lines.append("  sample journal lines:")
        for s in report.notable_lines[:3]:
            lines.append(f"    · {s}")
    if playbook_lines:
        lines.append("  playbooks:")
        lines.extend(playbook_lines)
    if baseline_line:
        lines.append(baseline_line)
    return "\n".join(lines)


async def post_to_slack(token: str, channel: str, text: str) -> None:
    """POST chat.postMessage. Failures are swallowed (logged) so a Slack
    outage never crashes the doctor loop."""
    if not token:
        logger.warning("doctor: no Slack token configured; skipping post")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                content=json.dumps({"channel": channel, "text": text}).encode(),
            )
            body = resp.json()
            if not body.get("ok"):
                logger.warning("doctor: slack post not-ok: %s", body)
    except Exception:
        logger.exception("doctor: slack post raised; continuing")


class AlfredDoctorOrchestrator:
    """Long-running surveillance orchestrator (Phase 1 — read-only).

    Lifecycle of a single kickoff:
      1. Parse payload (interval, channel, last_scan_ts).
      2. Run one scan (mesh + journal + linear).
      3. Format + post Slack digest.
      4. Sleep ``interval_seconds``.
      5. Queue next doctor kickoff with ``last_scan_ts = now``.
      6. Mark current mesh task completed.

    A daemon restart that kills the in-flight task simply means the
    *previously queued* next-kickoff is the heir; the chain self-heals.
    """

    def __init__(
        self,
        *,
        task: dict,
        persona,
        mesh,
        soul,
        dispatcher,
        settings,
    ) -> None:
        self.task = task
        self.task_id = task["id"]
        self.persona = persona
        self.mesh = mesh
        self.soul = soul
        self.dispatcher = dispatcher
        self.settings = settings
        self.payload: dict[str, Any] = {}
        self.interval_seconds = DEFAULT_INTERVAL_SECONDS
        self.slack_channel = DEFAULT_SLACK_CHANNEL

    async def run(self) -> None:
        """Top-level lifecycle. Broad try/except so a doctor crash always
        marks the kickoff failed (the next kickoff in the chain is what
        keeps the surveillance alive)."""
        logger.info("alfred-doctor starting (task=%s)", self.task_id)
        try:
            await self._run_inner()
        except Exception as e:  # noqa: BLE001 — top-level sink intentional
            logger.exception("alfred-doctor crashed")
            try:
                await self.mesh.complete(
                    self.task_id,
                    session_id=self.settings.soul_session_id,
                    status="failed",
                    result={"error": f"doctor crashed: {type(e).__name__}: {str(e)[:300]}"},
                )
            except Exception:
                logger.exception("doctor: failed to mark kickoff failed")

    def _parse_payload(self) -> None:
        raw = self.task.get("description") or ""
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        self.payload = payload
        self.interval_seconds = _parse_interval_seconds(payload)
        self.slack_channel = _parse_slack_channel(payload)
        try:
            self.last_scan_ts = float(payload.get("last_scan_ts") or 0.0)
        except (TypeError, ValueError):
            self.last_scan_ts = 0.0
        # Cap lookback so we never scan more than 1h of history even if a
        # chain link was missed.
        floor = time.time() - 3600.0
        if self.last_scan_ts < floor:
            self.last_scan_ts = floor

    async def _run_inner(self) -> None:
        self._parse_payload()

        report = ScanReport(started_at=time.time())
        await scan_mesh_recent_tasks(
            self.mesh, since_ts=self.last_scan_ts, report=report,
        )
        scan_journal_via_subprocess(
            lookback_seconds=DEFAULT_JOURNAL_LOOKBACK_SECONDS,
            report=report,
        )
        linear_key = os.getenv("LINEAR_API_KEY") or getattr(
            self.settings, "linear_api_key", "",
        )
        await scan_linear_grounding_gaps(
            linear_api_key=linear_key,
            since_ts=self.last_scan_ts,
            report=report,
        )

        playbook_results = await self._run_playbooks(linear_key=linear_key)
        report.finished_at = time.time()

        # Phase 3a: capture this tick's metric snapshot + compute the
        # rolling-window baseline. Read-only — no corrective action yet
        # (Phase 3b lifts the baseline-deviation signal into action).
        # All I/O is best-effort, swallowed exceptions, never breaks the
        # surveillance loop.
        baseline_line = ""
        try:
            metrics_path = metrics_path_from_env()
            snapshot = build_snapshot_from_doctor(
                started_at=report.started_at,
                finished_at=report.finished_at,
                counters=report.counters,
                grounding_gaps=report.grounding_gaps,
                playbook_results=playbook_results,
                daemon_head=os.getenv("ALFRED_COO_HEAD", "")[:12],
            )
            record_snapshot(snapshot, path=metrics_path)
            recent = load_recent_snapshots(
                path=metrics_path,
                since_ts=time.time() - DEFAULT_BASELINE_WINDOW_SEC,
            )
            baseline = compute_baseline(recent)
            baseline_line = format_baseline_summary(
                baseline, n_snapshots=len(recent),
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "doctor: metric snapshot/baseline failed; continuing",
            )

        slack_token = (
            os.getenv("SLACK_BOT_TOKEN_ALFRED")
            or os.getenv("SLACK_BOT_TOKEN")
            or getattr(self.settings, "slack_bot_token", "")
        )
        message = format_slack_message(
            report,
            daemon_head=os.getenv("ALFRED_COO_HEAD", "")[:7],
            playbook_results=playbook_results,
            baseline_line=baseline_line,
        )
        await post_to_slack(slack_token, self.slack_channel, message)

        # Cadence sleep BEFORE queuing the next kickoff — gives this tick's
        # post a chance to land and prevents tight-loop on a misconfigured
        # interval.
        await asyncio.sleep(self.interval_seconds)
        await self._queue_next_kickoff(now_ts=report.finished_at)

        playbook_summary = {
            pr.kind: {
                "found": pr.candidates_found,
                "acted": pr.actions_taken,
                "skipped": pr.actions_skipped,
                "errors": len(pr.errors),
                "escalations": len(getattr(pr, "escalations", []) or []),
                "dry_run": pr.dry_run,
            }
            for pr in playbook_results
        }
        await self.mesh.complete(
            self.task_id,
            session_id=self.settings.soul_session_id,
            status="completed",
            result={
                "summary": (
                    f"doctor scan {len(report.findings)} findings; "
                    f"queued next tick"
                ),
                "counters": report.counters,
                "grounding_gaps": report.grounding_gaps[:20],
                "playbooks": playbook_summary,
            },
        )

    async def _run_playbooks(
        self, *, linear_key: str,
    ) -> list[PlaybookResult]:
        """Invoke each registered playbook, swallowing per-playbook errors.

        Playbooks are gated behind ``payload.playbooks_enabled=true`` so
        the surveillance loop can ship + bake before any mutation runs.
        ``payload.playbook_dry_run`` defaults to True (safety-first); set
        it false in the kickoff payload once dry-run digests look clean.
        """
        if self.payload.get("playbooks_enabled") is not True:
            return []
        dry_run = bool(self.payload.get("playbook_dry_run", True))
        results: list[PlaybookResult] = []
        for pb in DEFAULT_PLAYBOOKS:
            try:
                pr = await pb.execute(
                    linear_api_key=linear_key,
                    dry_run=dry_run,
                    mesh=self.mesh,
                )
                results.append(pr)
            except Exception as e:  # noqa: BLE001 — never break the loop
                logger.exception("doctor: playbook %s raised", pb.kind)
                results.append(PlaybookResult(
                    kind=pb.kind,
                    dry_run=dry_run,
                    errors=[f"{type(e).__name__}: {str(e)[:80]}"],
                ))
        # Always log a one-line per-playbook summary so the daemon journal
        # records playbook activity even when the Slack digest collapses
        # to ``Substrate quiet`` (i.e. all playbooks were silent). Without
        # this line, a healthy chain looks identical in journal output to
        # one whose playbooks-enabled flag silently isn't taking effect.
        if results:
            summary = ", ".join(
                f"{pr.kind}=found:{pr.candidates_found}/acted:{pr.actions_taken}"
                + (
                    f"/esc:{len(pr.escalations)}"
                    if getattr(pr, "escalations", None) else ""
                )
                + (f"/err:{len(pr.errors)}" if pr.errors else "")
                for pr in results
            )
            logger.info(
                "alfred-doctor playbooks: dry_run=%s %s", dry_run, summary,
            )
        return results

    async def _queue_next_kickoff(self, *, now_ts: float) -> None:
        """Self-perpetuate. The next kickoff carries the same config plus
        the timestamp this tick finished, so the next tick's mesh/Linear
        scans use the correct lookback floor."""
        next_payload = dict(self.payload)
        next_payload["interval_seconds"] = self.interval_seconds
        next_payload["slack_channel"] = self.slack_channel
        next_payload["last_scan_ts"] = now_ts
        next_payload["parent_doctor_task_id"] = self.task_id

        title = "[persona:alfred-doctor] surveillance tick"
        try:
            resp = await self.mesh.create_task(
                title=title,
                description=json.dumps(next_payload),
                from_session_id=self.settings.soul_session_id,
            )
        except Exception:
            logger.exception("doctor: failed to queue next tick")
            return
        if not isinstance(resp, dict) or not resp.get("id"):
            logger.warning("doctor: next-tick create_task returned no id: %r", resp)
            return
        logger.info(
            "alfred-doctor queued next tick %s (interval=%ds)",
            resp["id"], self.interval_seconds,
        )
