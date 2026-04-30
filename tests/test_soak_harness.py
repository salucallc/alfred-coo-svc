"""Unit tests for scripts/soak_harness.py (SAL-3716).

The harness shells out to systemctl/journalctl/ps; tests stub those
subprocess calls so the verdict + summary logic can be exercised
without a live daemon.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from scripts import soak_harness as sh


@pytest.fixture
def fake_run(monkeypatch):
    """Capture all subprocess.run calls and return programmable replies."""

    class FakeResult:
        def __init__(self, stdout: str = "", returncode: int = 0):
            self.stdout = stdout
            self.returncode = returncode

    state = {"queue": []}  # list of (matcher, FakeResult) tuples
    calls: list[list[str]] = []

    def fake_run(cmd, capture_output=False, text=False, timeout=None):  # noqa: ARG001
        calls.append(list(cmd))
        for matcher, reply in state["queue"]:
            if matcher(cmd):
                return reply
        return FakeResult("", 0)

    monkeypatch.setattr(sh.subprocess, "run", fake_run)
    return calls, state, FakeResult


def _enqueue(state, FakeResult, matcher, stdout: str, returncode: int = 0):
    state["queue"].append((matcher, FakeResult(stdout, returncode)))


def test_take_tick_active_with_pid_and_metrics(fake_run, tmp_path, monkeypatch):
    calls, state, FakeResult = fake_run
    _enqueue(state, FakeResult, lambda c: c[:2] == ["systemctl", "is-active"], "active\n")
    _enqueue(state, FakeResult, lambda c: c[:2] == ["systemctl", "show"], "MainPID=4242\n")
    _enqueue(state, FakeResult, lambda c: c[:2] == ["ps", "-p"], "12.5\n")
    _enqueue(state, FakeResult, lambda c: c[0] == "journalctl" and "-p" in c and c[c.index("-p") + 1] == "err", "err1\nerr2\n")
    _enqueue(state, FakeResult, lambda c: c[0] == "journalctl" and "-p" in c and c[c.index("-p") + 1] == "warning", "")

    proc_dir = tmp_path / "proc" / "4242"
    proc_dir.mkdir(parents=True)
    (proc_dir / "status").write_text("Name:\tx\nVmRSS:\t  204800 kB\n")
    monkeypatch.setattr(sh, "Path", lambda p: tmp_path / p.lstrip("/"))

    sample = sh.take_tick("alfred-coo", tick_seconds=60)
    assert sample.daemon_active is True
    assert sample.daemon_pid == 4242
    assert sample.rss_mb == 200.0  # 204800 kB / 1024
    assert sample.cpu_pct == 12.5
    assert sample.journal_errors_window == 2
    assert sample.journal_warns_window == 0


def test_take_tick_inactive_unit_skips_metric_calls(fake_run):
    _, state, FakeResult = fake_run
    _enqueue(state, FakeResult, lambda c: c[:2] == ["systemctl", "is-active"], "inactive\n")
    _enqueue(state, FakeResult, lambda c: c[0] == "journalctl", "")
    sample = sh.take_tick("alfred-coo", tick_seconds=60)
    assert sample.daemon_active is False
    assert sample.daemon_pid is None
    assert sample.rss_mb is None
    assert sample.cpu_pct is None


def test_summarize_hour_aggregates_metrics():
    base = 1_700_000_000.0
    ticks = [
        sh.TickSample(
            ts=base + i * 60.0,
            daemon_active=(i != 5),  # one tick down
            daemon_pid=4242 if i != 5 else None,
            rss_mb=100.0 + i if i != 5 else None,
            cpu_pct=10.0,
            journal_errors_window=(2 if i == 7 else 0),
            journal_warns_window=0,
        )
        for i in range(60)
    ]
    summary = sh.summarize_hour(hour_index=0, ticks=ticks, pid_changes=1)
    assert summary.ticks == 60
    assert summary.daemon_active_ticks == 59
    assert round(summary.daemon_active_pct, 2) == round(59 / 60 * 100, 2)
    assert summary.peak_rss_mb == 159.0
    assert summary.total_errors == 2
    assert summary.pid_changes == 1


def test_write_verdict_green_when_all_gates_pass(tmp_path):
    base = 1_700_000_000.0
    samples = [
        sh.TickSample(
            ts=base + i * 60.0,
            daemon_active=True,
            daemon_pid=4242,
            rss_mb=200.0,
            cpu_pct=5.0,
            journal_errors_window=0,
            journal_warns_window=0,
        )
        for i in range(180)
    ]
    run = sh.SoakRun(
        start_ts=base,
        duration_seconds=180 * 60,
        tick_seconds=60,
        output_dir=tmp_path,
        samples=samples,
        last_pid=4242,
        pid_change_count=0,
    )
    verdict, failing = sh.write_verdict(
        run,
        min_active_pct=99.0,
        max_errors_per_min=0.5,
        allow_restart=False,
    )
    assert verdict == "GREEN"
    assert failing == []
    assert "GREEN" in run.verdict_path.read_text()


def test_write_verdict_red_when_error_rate_exceeded(tmp_path):
    base = 1_700_000_000.0
    samples = [
        sh.TickSample(
            ts=base + i * 60.0,
            daemon_active=True,
            daemon_pid=4242,
            rss_mb=200.0,
            cpu_pct=5.0,
            journal_errors_window=10,  # 10 err/min, gate is 0.5
            journal_warns_window=0,
        )
        for i in range(60)
    ]
    run = sh.SoakRun(
        start_ts=base,
        duration_seconds=60 * 60,
        tick_seconds=60,
        output_dir=tmp_path,
        samples=samples,
        last_pid=4242,
        pid_change_count=0,
    )
    verdict, failing = sh.write_verdict(
        run,
        min_active_pct=99.0,
        max_errors_per_min=0.5,
        allow_restart=False,
    )
    assert verdict == "RED"
    assert any("errors_per_min" in f for f in failing)


def test_write_verdict_red_on_pid_change_unless_allowed(tmp_path):
    base = 1_700_000_000.0
    samples = [
        sh.TickSample(
            ts=base + i * 60.0,
            daemon_active=True,
            daemon_pid=4242,
            rss_mb=200.0,
            cpu_pct=5.0,
            journal_errors_window=0,
            journal_warns_window=0,
        )
        for i in range(60)
    ]
    run_strict = sh.SoakRun(
        start_ts=base, duration_seconds=3600, tick_seconds=60,
        output_dir=tmp_path, samples=samples,
        last_pid=4242, pid_change_count=1,
    )
    verdict_strict, failing_strict = sh.write_verdict(
        run_strict, min_active_pct=99.0, max_errors_per_min=0.5,
        allow_restart=False,
    )
    assert verdict_strict == "RED"
    assert any("pid_change_count" in f for f in failing_strict)

    lenient_dir = tmp_path / "lenient"
    lenient_dir.mkdir()
    run_lenient = sh.SoakRun(
        start_ts=base, duration_seconds=3600, tick_seconds=60,
        output_dir=lenient_dir, samples=samples,
        last_pid=4242, pid_change_count=1,
    )
    verdict_lenient, failing_lenient = sh.write_verdict(
        run_lenient, min_active_pct=99.0, max_errors_per_min=0.5,
        allow_restart=True,
    )
    assert verdict_lenient == "GREEN"
    assert failing_lenient == []


def test_write_verdict_red_on_no_samples(tmp_path):
    run = sh.SoakRun(
        start_ts=time.time(), duration_seconds=60, tick_seconds=60,
        output_dir=tmp_path,
    )
    verdict, failing = sh.write_verdict(
        run, min_active_pct=99.0, max_errors_per_min=0.5, allow_restart=False,
    )
    assert verdict == "RED"
    assert "no samples captured" in failing


def test_run_soak_writes_files_and_returns_exit_code(monkeypatch, tmp_path):
    """Smoke test: run the loop for a tiny duration with stubbed ticks."""
    counter = {"n": 0}

    def fast_take_tick(unit: str, tick_seconds: int) -> sh.TickSample:  # noqa: ARG001
        counter["n"] += 1
        return sh.TickSample(
            ts=time.time(),
            daemon_active=True,
            daemon_pid=4242,
            rss_mb=150.0,
            cpu_pct=4.0,
            journal_errors_window=0,
            journal_warns_window=0,
        )

    monkeypatch.setattr(sh, "take_tick", fast_take_tick)

    rc = sh.run_soak(
        unit="alfred-coo",
        duration_hours=2.0 / 3600.0,  # 2 seconds
        tick_seconds=1,
        output_dir=tmp_path,
        min_active_pct=99.0,
        max_errors_per_min=0.5,
        allow_restart=False,
    )
    assert rc == 0  # GREEN
    assert counter["n"] >= 1

    tick_files = list(tmp_path.glob("soak_*.jsonl"))
    assert any("hourly" not in str(f) for f in tick_files)
    verdict_files = list(tmp_path.glob("soak_*_verdict.md"))
    assert verdict_files
    body = verdict_files[0].read_text()
    assert "GREEN" in body
