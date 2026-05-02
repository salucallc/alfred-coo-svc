"""Alfred-doctor metric stream + baseline computation (Phase 3a, read-only).

Each doctor tick produces a ``MetricSnapshot`` capturing scan duration,
failure-class counters, per-playbook summary, grounding-gap count, and
the daemon HEAD at the time. The snapshot is:

1. **Appended to a local JSONL file** at ``/var/lib/alfred-coo/doctor_metrics.jsonl``
   (configurable). The doctor reads recent snapshots from this file to
   compute rolling-window baselines (P5/P50/P95). Local-disk storage is
   chosen over a remote DB because: (a) per-tick latency matters less
   than the doctor's resilience to network partitions; (b) the file is
   ~140 KB/day (288 ticks × ~500 B), easily 7d in <1 MB; (c) Phase 3
   primary read pattern is "last N days" — sequential tail scan is fine.
2. **Emitted as a structured INFO log line** with logger
   ``alfred_coo.autonomous_build.doctor_metrics`` so promtail/Loki pick
   it up automatically and Grafana can graph the same metrics without
   the dashboard having to know where the JSONL lives.

This module is read-only with respect to the daemon's behavior — it
never triggers corrective action. That's Phase 3b. The Slack digest
gains a one-line "baseline soak: N snapshots over Wd window
(scan p50=Xs p95=Ys)" footer so the operator can watch the baseline
populate without it being noisy.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("alfred_coo.autonomous_build.doctor_metrics")


DEFAULT_METRICS_PATH = "/var/lib/alfred-coo/doctor_metrics.jsonl"
DEFAULT_BASELINE_WINDOW_SEC = 7 * 24 * 3600  # 7d sliding window
DEFAULT_BASELINE_MIN_SAMPLES = 12             # ~1h at 5min cadence
DEFAULT_LOAD_LIMIT = 4096                     # safety cap on tail scan


@dataclass
class MetricSnapshot:
    """One doctor tick's measurement record.

    Schema is intentionally flat + JSON-friendly so promtail can index
    it without a parser stage and ``compute_baseline`` can fan keys
    directly into per-metric series.
    """

    timestamp: float                    # unix epoch (seconds, float)
    scan_duration_s: float              # finished_at - started_at
    counters: dict[str, int] = field(default_factory=dict)
    grounding_gaps_count: int = 0
    playbook_summary: dict[str, dict[str, Any]] = field(default_factory=dict)
    daemon_head: str = ""

    def to_jsonl_line(self) -> str:
        """Render as a single JSONL line (no embedded newlines)."""
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: dict) -> "MetricSnapshot":
        """Tolerant loader — drops unknown keys, fills missing with defaults
        so an older-schema record on disk doesn't break baseline computation
        after a code update."""
        return cls(
            timestamp=float(d.get("timestamp") or 0.0),
            scan_duration_s=float(d.get("scan_duration_s") or 0.0),
            counters=dict(d.get("counters") or {}),
            grounding_gaps_count=int(d.get("grounding_gaps_count") or 0),
            playbook_summary=dict(d.get("playbook_summary") or {}),
            daemon_head=str(d.get("daemon_head") or ""),
        )


def record_snapshot(
    snapshot: MetricSnapshot,
    *,
    path: str = DEFAULT_METRICS_PATH,
) -> None:
    """Append ``snapshot`` to the JSONL file AND emit a structured INFO
    log line so Loki/Grafana pick it up automatically.

    Best-effort: any I/O error is logged and swallowed — the doctor
    surveillance loop must never stall on metric I/O.
    """
    line = snapshot.to_jsonl_line()
    # JSONL write — append-only, line-delimited so concurrent readers can
    # tail-and-parse without locking.
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        logger.exception(
            "doctor_metrics: jsonl append failed (path=%s); continuing", path,
        )

    # Loki-bound log line. The structured fields land in the log record's
    # ``extra`` and our JSON formatter (configured in alfred_coo.log)
    # serialises them. ``metric_event=tick`` is the discriminator label
    # the Grafana dashboard queries on.
    try:
        logger.info(
            "doctor_metrics tick scan=%.2fs counters=%d gaps=%d head=%s",
            snapshot.scan_duration_s,
            sum(snapshot.counters.values()),
            snapshot.grounding_gaps_count,
            snapshot.daemon_head or "?",
            extra={
                "metric_event": "tick",
                "metric_scan_duration_s": snapshot.scan_duration_s,
                "metric_counters_total": sum(snapshot.counters.values()),
                "metric_grounding_gaps": snapshot.grounding_gaps_count,
                "metric_daemon_head": snapshot.daemon_head,
                "metric_counters": snapshot.counters,
                "metric_playbooks": snapshot.playbook_summary,
            },
        )
    except Exception:  # noqa: BLE001
        # Logger crashes are spectacularly rare but don't let them break
        # the doctor loop.
        logger.exception("doctor_metrics: structured log emit failed")


def load_recent_snapshots(
    *,
    path: str = DEFAULT_METRICS_PATH,
    since_ts: float | None = None,
    limit: int = DEFAULT_LOAD_LIMIT,
) -> list[MetricSnapshot]:
    """Load up to ``limit`` recent snapshots from the JSONL file.

    Tail-reads efficiently for the common "last N days" case: caller
    sets ``since_ts = time.time() - 7*86400`` and gets back snapshots
    in append order (oldest first).

    Best-effort: missing file → empty list; unparseable lines are
    skipped with a warning. Never raises into the doctor loop.
    """
    p = Path(path)
    if not p.exists():
        return []
    out: list[MetricSnapshot] = []
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "doctor_metrics: jsonl read failed (path=%s): %s",
            path, type(e).__name__,
        )
        return []
    # Walk backward from EOF so we can stop when ``since_ts`` is reached
    # without parsing the entire history. Reverse the result at the end
    # so callers get oldest-first ordering.
    skipped = 0
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            skipped += 1
            continue
        snap = MetricSnapshot.from_dict(d)
        if since_ts is not None and snap.timestamp < since_ts:
            break
        out.append(snap)
        if len(out) >= limit:
            break
    if skipped:
        logger.debug(
            "doctor_metrics: skipped %d unparseable lines in %s",
            skipped, path,
        )
    out.reverse()
    return out


@dataclass
class BaselineMetric:
    """One metric's rolling-window distribution summary."""
    n: int
    p5: float
    p50: float
    p95: float
    mean: float


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Compute percentile from a pre-sorted list. Linear interpolation,
    matches numpy's default. ``sorted_vals`` MUST be sorted ascending."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = rank - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def compute_baseline(
    snapshots: Iterable[MetricSnapshot],
    *,
    metrics: tuple[str, ...] = (
        "scan_duration_s",
        "grounding_gaps_count",
    ),
    counter_keys: tuple[str, ...] = (
        "silent_with_tools",
        "grounding_gap_escalation",
        "hard_timeout",
        "journal_wave_retry_fired",
        "journal_silent_with_tools",
        "journal_wave_gate_failed",
    ),
) -> dict[str, BaselineMetric]:
    """Compute per-metric distribution summary over the supplied snapshots.

    ``metrics`` are flat-attribute fields on ``MetricSnapshot``.
    ``counter_keys`` are dotted into ``MetricSnapshot.counters`` with
    a default of 0 when the key is absent (so a tick that reported
    no ``silent_with_tools`` events contributes a 0 to the baseline,
    matching the operator's intuition that quiet ticks ARE part of
    normal behavior).

    Returns an empty dict when fewer than ``DEFAULT_BASELINE_MIN_SAMPLES``
    snapshots are available; the caller should treat that as "still
    soaking" and not yet emit baseline-relative info.
    """
    snaps = list(snapshots)
    if len(snaps) < DEFAULT_BASELINE_MIN_SAMPLES:
        return {}

    out: dict[str, BaselineMetric] = {}

    for key in metrics:
        vals = sorted(float(getattr(s, key, 0.0) or 0.0) for s in snaps)
        out[key] = BaselineMetric(
            n=len(vals),
            p5=_percentile(vals, 5),
            p50=_percentile(vals, 50),
            p95=_percentile(vals, 95),
            mean=statistics.fmean(vals),
        )

    for ckey in counter_keys:
        vals = sorted(float((s.counters or {}).get(ckey, 0)) for s in snaps)
        out[f"counters.{ckey}"] = BaselineMetric(
            n=len(vals),
            p5=_percentile(vals, 5),
            p50=_percentile(vals, 50),
            p95=_percentile(vals, 95),
            mean=statistics.fmean(vals),
        )

    return out


def format_baseline_summary(
    baseline: dict[str, BaselineMetric],
    *,
    n_snapshots: int,
    window_sec: int = DEFAULT_BASELINE_WINDOW_SEC,
) -> str:
    """One-line summary the doctor folds into the Slack digest.

    ``baseline`` empty (still soaking) → returns a "soak progress" line
    so the operator sees the metric stream is alive. Otherwise renders
    the most operator-relevant percentiles.
    """
    if not baseline:
        return (
            f"  baseline soak: {n_snapshots} snapshots so far "
            f"(need {DEFAULT_BASELINE_MIN_SAMPLES} min for baseline)"
        )
    scan = baseline.get("scan_duration_s")
    if scan is None:
        return f"  baseline: n={n_snapshots} (no scan_duration_s metric)"
    window_h = window_sec // 3600
    return (
        f"  baseline ({n_snapshots} snapshots over {window_h}h window): "
        f"scan p50={scan.p50:.1f}s p95={scan.p95:.1f}s"
    )


def build_snapshot_from_doctor(
    *,
    started_at: float,
    finished_at: float,
    counters: dict[str, int],
    grounding_gaps: list[str],
    playbook_results: list,
    daemon_head: str = "",
) -> MetricSnapshot:
    """Helper the doctor calls inside ``_run_inner`` to assemble a
    snapshot from the data it already has on hand.

    ``playbook_results`` is the list of ``PlaybookResult`` objects
    returned by ``_run_playbooks``; we summarise each into a small
    dict so the JSONL line stays compact.
    """
    pb_summary: dict[str, dict[str, Any]] = {}
    for pr in playbook_results:
        kind = getattr(pr, "kind", "unknown")
        pb_summary[kind] = {
            "found": int(getattr(pr, "candidates_found", 0)),
            "acted": int(getattr(pr, "actions_taken", 0)),
            "skipped": int(getattr(pr, "actions_skipped", 0)),
            "errors": len(getattr(pr, "errors", []) or []),
            "escalations": len(getattr(pr, "escalations", []) or []),
            "dry_run": bool(getattr(pr, "dry_run", False)),
        }
    return MetricSnapshot(
        timestamp=finished_at,
        scan_duration_s=max(0.0, finished_at - started_at),
        counters=dict(counters),
        grounding_gaps_count=len(grounding_gaps or []),
        playbook_summary=pb_summary,
        daemon_head=str(daemon_head)[:12] if daemon_head else "",
    )


def metrics_path_from_env() -> str:
    """Allow operators to override via ``ALFRED_DOCTOR_METRICS_PATH`` env
    var (e.g., for tests or non-default state dirs)."""
    return os.environ.get("ALFRED_DOCTOR_METRICS_PATH", DEFAULT_METRICS_PATH)
