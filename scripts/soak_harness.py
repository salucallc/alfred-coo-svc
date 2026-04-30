"""48-hour validation soak harness for alfred-coo-svc (SAL-3716).

Continuously samples daemon health + autonomous-build pipeline metrics
for a configurable duration (default 48h) and emits a green/red verdict
at the end. Designed to run on the same host as the daemon (Oracle VM)
so process-level metrics are available without remoting.

Usage::

    python scripts/soak_harness.py \\
        --duration-hours 48 \\
        --tick-seconds 60 \\
        --output-dir /opt/alfred-coo/soak_runs

Output layout under ``--output-dir``:

* ``soak_<start_ts>.jsonl``   — one JSON object per tick (raw samples)
* ``soak_<start_ts>_hourly.jsonl`` — one summary per hour
* ``soak_<start_ts>_verdict.md``   — final markdown report

Verdict gates (configurable via CLI):

* ``daemon_active_pct >= 99.0``  (daemon up substantially the whole soak)
* ``errors_per_min <= 0.5``      (journalctl ERROR rate stays bounded)
* ``no_pid_change``              (no daemon restart unless --allow-restart)

When all gates pass the verdict is GREEN; any failed gate makes it RED
and lists the failing gate(s). Exit code is 0 for GREEN, 1 for RED so
the harness can gate downstream automation (e.g. a v1.0.0 GA promote).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class TickSample:
    """One periodic sample of daemon + pipeline state."""

    ts: float
    daemon_active: bool
    daemon_pid: Optional[int]
    rss_mb: Optional[float]
    cpu_pct: Optional[float]
    journal_errors_window: int
    journal_warns_window: int

    def to_json(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True)


@dataclass
class HourlySummary:
    hour_index: int
    ts_start: float
    ts_end: float
    ticks: int
    daemon_active_ticks: int
    pid_changes: int
    avg_rss_mb: Optional[float]
    peak_rss_mb: Optional[float]
    total_errors: int
    total_warns: int

    @property
    def daemon_active_pct(self) -> float:
        return 100.0 * self.daemon_active_ticks / max(1, self.ticks)

    def to_json(self) -> str:
        d = self.__dict__.copy()
        d["daemon_active_pct"] = round(self.daemon_active_pct, 2)
        return json.dumps(d, sort_keys=True)


@dataclass
class SoakRun:
    start_ts: float
    duration_seconds: int
    tick_seconds: int
    output_dir: Path
    samples: list[TickSample] = field(default_factory=list)
    hourly: list[HourlySummary] = field(default_factory=list)
    last_pid: Optional[int] = None
    pid_change_count: int = 0
    stop_requested: bool = False

    @property
    def tick_path(self) -> Path:
        return self.output_dir / f"soak_{int(self.start_ts)}.jsonl"

    @property
    def hourly_path(self) -> Path:
        return self.output_dir / f"soak_{int(self.start_ts)}_hourly.jsonl"

    @property
    def verdict_path(self) -> Path:
        return self.output_dir / f"soak_{int(self.start_ts)}_verdict.md"


def _systemctl_active(unit: str) -> bool:
    """Return True iff the systemd unit is currently active."""
    try:
        r = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() == "active"
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def _systemctl_main_pid(unit: str) -> Optional[int]:
    """Return the MainPID of the unit, or None if not running / not found."""
    try:
        r = subprocess.run(
            ["systemctl", "show", unit, "--property=MainPID"],
            capture_output=True, text=True, timeout=10,
        )
        for line in r.stdout.splitlines():
            if line.startswith("MainPID="):
                pid = int(line.split("=", 1)[1])
                return pid if pid > 0 else None
    except (subprocess.SubprocessError, FileNotFoundError, ValueError):
        return None
    return None


def _process_rss_mb(pid: int) -> Optional[float]:
    """Return RSS in MB for ``pid`` by reading /proc/<pid>/status."""
    path = Path(f"/proc/{pid}/status")
    if not path.exists():
        return None
    try:
        for line in path.read_text().splitlines():
            if line.startswith("VmRSS:"):
                # Format: "VmRSS:    123456 kB"
                kb = int(line.split()[1])
                return round(kb / 1024.0, 2)
    except (OSError, ValueError):
        return None
    return None


def _process_cpu_pct(pid: int) -> Optional[float]:
    """Snapshot CPU% for the process using ps (cheap, no psutil dep)."""
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "%cpu="],
            capture_output=True, text=True, timeout=5,
        )
        v = r.stdout.strip()
        return float(v) if v else None
    except (subprocess.SubprocessError, ValueError, FileNotFoundError):
        return None


def _journal_event_count(unit: str, since_seconds: int, level: str) -> int:
    """Count journalctl entries at ``level`` for the unit in the window."""
    since = f"{since_seconds} seconds ago"
    try:
        r = subprocess.run(
            [
                "journalctl",
                "-u", unit,
                "--since", since,
                "-p", level,
                "--no-pager",
                "-q",
            ],
            capture_output=True, text=True, timeout=15,
        )
        # Each event is one line (assuming default formatter).
        return sum(1 for ln in r.stdout.splitlines() if ln.strip())
    except (subprocess.SubprocessError, FileNotFoundError):
        return 0


def take_tick(unit: str, tick_seconds: int) -> TickSample:
    """Capture a single tick of state for ``unit``."""
    now = time.time()
    active = _systemctl_active(unit)
    pid = _systemctl_main_pid(unit) if active else None
    rss = _process_rss_mb(pid) if pid else None
    cpu = _process_cpu_pct(pid) if pid else None
    err_count = _journal_event_count(unit, tick_seconds, "err")
    warn_count = _journal_event_count(unit, tick_seconds, "warning")
    return TickSample(
        ts=now,
        daemon_active=active,
        daemon_pid=pid,
        rss_mb=rss,
        cpu_pct=cpu,
        journal_errors_window=err_count,
        journal_warns_window=warn_count,
    )


def summarize_hour(
    hour_index: int,
    ticks: list[TickSample],
    pid_changes: int,
) -> HourlySummary:
    rss_values = [t.rss_mb for t in ticks if t.rss_mb is not None]
    return HourlySummary(
        hour_index=hour_index,
        ts_start=ticks[0].ts,
        ts_end=ticks[-1].ts,
        ticks=len(ticks),
        daemon_active_ticks=sum(1 for t in ticks if t.daemon_active),
        pid_changes=pid_changes,
        avg_rss_mb=round(sum(rss_values) / len(rss_values), 2) if rss_values else None,
        peak_rss_mb=max(rss_values) if rss_values else None,
        total_errors=sum(t.journal_errors_window for t in ticks),
        total_warns=sum(t.journal_warns_window for t in ticks),
    )


def write_verdict(
    run: SoakRun,
    *,
    min_active_pct: float,
    max_errors_per_min: float,
    allow_restart: bool,
) -> tuple[str, list[str]]:
    """Compute verdict + return (verdict, failing_gates)."""
    if not run.samples:
        return "RED", ["no samples captured"]
    total_ticks = len(run.samples)
    active_ticks = sum(1 for s in run.samples if s.daemon_active)
    active_pct = 100.0 * active_ticks / total_ticks
    minutes_elapsed = (run.samples[-1].ts - run.samples[0].ts) / 60.0
    total_errors = sum(s.journal_errors_window for s in run.samples)
    errors_per_min = total_errors / max(1.0, minutes_elapsed)

    failing: list[str] = []
    if active_pct < min_active_pct:
        failing.append(
            f"daemon_active_pct={active_pct:.2f} < {min_active_pct}"
        )
    if errors_per_min > max_errors_per_min:
        failing.append(
            f"errors_per_min={errors_per_min:.3f} > {max_errors_per_min}"
        )
    if run.pid_change_count > 0 and not allow_restart:
        failing.append(
            f"pid_change_count={run.pid_change_count} (use --allow-restart)"
        )

    verdict = "GREEN" if not failing else "RED"
    body = [
        f"# Soak Verdict: **{verdict}**",
        "",
        f"- Start: `{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(run.start_ts))}`",
        f"- Duration: {run.duration_seconds // 3600}h "
        f"(actual elapsed {minutes_elapsed:.1f} min)",
        f"- Ticks: {total_ticks}",
        f"- Daemon active: {active_pct:.2f}% (gate: >= {min_active_pct}%)",
        f"- Errors total: {total_errors} "
        f"({errors_per_min:.3f}/min, gate: <= {max_errors_per_min}/min)",
        f"- PID changes: {run.pid_change_count} "
        f"(gate: {0 if not allow_restart else 'allowed'})",
        f"- Output: `{run.tick_path.name}` + `{run.hourly_path.name}`",
        "",
    ]
    if failing:
        body.append("## Failing gates")
        body.extend(f"- {g}" for g in failing)
    else:
        body.append("All gates passed.")
    run.verdict_path.write_text("\n".join(body) + "\n")
    return verdict, failing


def run_soak(
    *,
    unit: str,
    duration_hours: float,
    tick_seconds: int,
    output_dir: Path,
    min_active_pct: float,
    max_errors_per_min: float,
    allow_restart: bool,
) -> int:
    """Main soak loop. Returns process exit code (0 GREEN, 1 RED)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    run = SoakRun(
        start_ts=time.time(),
        duration_seconds=int(duration_hours * 3600),
        tick_seconds=tick_seconds,
        output_dir=output_dir,
    )

    def _request_stop(signum, frame):  # noqa: ARG001
        run.stop_requested = True

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    print(
        f"[soak] starting unit={unit} duration={duration_hours}h "
        f"tick={tick_seconds}s output={run.tick_path}",
        flush=True,
    )

    end_ts = run.start_ts + run.duration_seconds
    hour_buffer: list[TickSample] = []
    hour_pid_changes = 0
    hour_index = 0
    next_hour_boundary = run.start_ts + 3600.0

    with run.tick_path.open("a") as tick_fp, run.hourly_path.open("a") as hour_fp:
        while not run.stop_requested:
            now = time.time()
            if now >= end_ts:
                break
            sample = take_tick(unit, tick_seconds)
            run.samples.append(sample)
            hour_buffer.append(sample)
            tick_fp.write(sample.to_json() + "\n")
            tick_fp.flush()

            if sample.daemon_pid is not None:
                if run.last_pid is not None and sample.daemon_pid != run.last_pid:
                    run.pid_change_count += 1
                    hour_pid_changes += 1
                run.last_pid = sample.daemon_pid

            if now >= next_hour_boundary and hour_buffer:
                summary = summarize_hour(hour_index, hour_buffer, hour_pid_changes)
                run.hourly.append(summary)
                hour_fp.write(summary.to_json() + "\n")
                hour_fp.flush()
                print(
                    f"[soak] hour {hour_index}: active={summary.daemon_active_pct:.1f}% "
                    f"errs={summary.total_errors} pid_changes={summary.pid_changes}",
                    flush=True,
                )
                hour_index += 1
                hour_buffer = []
                hour_pid_changes = 0
                next_hour_boundary += 3600.0

            elapsed_in_tick = time.time() - now
            sleep_for = max(0.0, tick_seconds - elapsed_in_tick)
            slept = 0.0
            while slept < sleep_for and not run.stop_requested:
                step = min(1.0, sleep_for - slept)
                time.sleep(step)
                slept += step

    if hour_buffer:
        summary = summarize_hour(hour_index, hour_buffer, hour_pid_changes)
        run.hourly.append(summary)
        with run.hourly_path.open("a") as hour_fp:
            hour_fp.write(summary.to_json() + "\n")

    verdict, failing = write_verdict(
        run,
        min_active_pct=min_active_pct,
        max_errors_per_min=max_errors_per_min,
        allow_restart=allow_restart,
    )
    print(f"[soak] verdict={verdict} failing={failing}", flush=True)
    return 0 if verdict == "GREEN" else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="alfred-coo-svc 48h validation soak")
    p.add_argument("--unit", default="alfred-coo")
    p.add_argument("--duration-hours", type=float, default=48.0)
    p.add_argument("--tick-seconds", type=int, default=60)
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/opt/alfred-coo/soak_runs"),
    )
    p.add_argument("--min-active-pct", type=float, default=99.0)
    p.add_argument("--max-errors-per-min", type=float, default=0.5)
    p.add_argument(
        "--allow-restart",
        action="store_true",
        help="Don't fail the verdict on daemon PID changes (e.g. expected redeploys)",
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    args = parse_args(argv)
    if shutil.which("systemctl") is None:
        print(
            "[soak] systemctl not on PATH — this harness expects to run on the "
            "Oracle host alongside the daemon",
            file=sys.stderr,
        )
        return 2
    return run_soak(
        unit=args.unit,
        duration_hours=args.duration_hours,
        tick_seconds=args.tick_seconds,
        output_dir=args.output_dir,
        min_active_pct=args.min_active_pct,
        max_errors_per_min=args.max_errors_per_min,
        allow_restart=args.allow_restart,
    )


if __name__ == "__main__":
    raise SystemExit(main())
