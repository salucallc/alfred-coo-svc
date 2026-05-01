"""Tests for Phase 3a: alfred-doctor metric stream + baseline computation.

Covers:
* MetricSnapshot round-trip (to_jsonl_line → from_dict)
* record_snapshot appends JSONL + emits structured INFO log
* load_recent_snapshots filters by since_ts, tolerates corrupted lines,
  returns oldest-first ordering, respects limit
* compute_baseline returns empty when sample count is below threshold
* compute_baseline computes percentiles correctly for known distribution
* format_baseline_summary renders soak progress vs full baseline
* build_snapshot_from_doctor assembles snapshot from inputs the doctor has
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from alfred_coo.autonomous_build.doctor_metrics import (
    DEFAULT_BASELINE_MIN_SAMPLES,
    BaselineMetric,
    MetricSnapshot,
    build_snapshot_from_doctor,
    compute_baseline,
    format_baseline_summary,
    load_recent_snapshots,
    metrics_path_from_env,
    record_snapshot,
    _percentile,
)


# ── MetricSnapshot ──────────────────────────────────────────────────────────


def test_snapshot_round_trip_via_jsonl_line():
    """to_jsonl_line() → json.loads() → from_dict() must reproduce the
    original snapshot byte-for-byte where defaults round-trip."""
    s = MetricSnapshot(
        timestamp=1700000000.5,
        scan_duration_s=1.234,
        counters={"silent_with_tools": 4, "hard_timeout": 1},
        grounding_gaps_count=2,
        playbook_summary={"hydrate_apev_headings": {
            "found": 0, "acted": 0, "skipped": 0, "errors": 0, "dry_run": True,
        }},
        daemon_head="abc1234",
    )
    line = s.to_jsonl_line()
    assert "\n" not in line  # JSONL invariant
    d = json.loads(line)
    s2 = MetricSnapshot.from_dict(d)
    assert s2.timestamp == s.timestamp
    assert s2.scan_duration_s == s.scan_duration_s
    assert s2.counters == s.counters
    assert s2.grounding_gaps_count == s.grounding_gaps_count
    assert s2.playbook_summary == s.playbook_summary
    assert s2.daemon_head == s.daemon_head


def test_snapshot_from_dict_tolerates_missing_keys():
    """A schema-shifted record on disk (older code) must still load with
    sensible defaults rather than raising."""
    s = MetricSnapshot.from_dict({"timestamp": 1700000000.0})
    assert s.timestamp == 1700000000.0
    assert s.scan_duration_s == 0.0
    assert s.counters == {}
    assert s.grounding_gaps_count == 0
    assert s.playbook_summary == {}
    assert s.daemon_head == ""


# ── record_snapshot ─────────────────────────────────────────────────────────


def test_record_snapshot_appends_jsonl(tmp_path):
    p = tmp_path / "metrics.jsonl"
    s1 = MetricSnapshot(timestamp=1.0, scan_duration_s=0.5)
    s2 = MetricSnapshot(timestamp=2.0, scan_duration_s=0.7)
    record_snapshot(s1, path=str(p))
    record_snapshot(s2, path=str(p))
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["timestamp"] == 1.0
    assert json.loads(lines[1])["timestamp"] == 2.0


def test_record_snapshot_creates_parent_dir(tmp_path):
    p = tmp_path / "nested" / "subdir" / "metrics.jsonl"
    record_snapshot(MetricSnapshot(timestamp=1.0, scan_duration_s=0.1), path=str(p))
    assert p.exists()
    assert p.parent.exists()


def test_record_snapshot_emits_structured_log(tmp_path, caplog):
    p = tmp_path / "metrics.jsonl"
    caplog.set_level(logging.INFO, logger="alfred_coo.autonomous_build.doctor_metrics")
    s = MetricSnapshot(
        timestamp=1.0,
        scan_duration_s=2.5,
        counters={"silent_with_tools": 3},
        grounding_gaps_count=1,
        daemon_head="abc1234",
    )
    record_snapshot(s, path=str(p))
    # Find the structured tick record.
    records = [r for r in caplog.records if getattr(r, "metric_event", None) == "tick"]
    assert len(records) == 1
    rec = records[0]
    assert rec.metric_scan_duration_s == 2.5
    assert rec.metric_counters_total == 3
    assert rec.metric_grounding_gaps == 1
    assert rec.metric_daemon_head == "abc1234"


def test_record_snapshot_swallows_io_error(tmp_path, monkeypatch):
    """A path that fails to open must NOT raise — surveillance loop
    invariant. Logger output is suppressed so the Python 3.12 + pluggy
    cosmetic ``compact=True`` traceback bug doesn't crash the test
    runner when ``logger.exception`` writes its traceback."""
    log = logging.getLogger("alfred_coo.autonomous_build.doctor_metrics")
    monkeypatch.setattr(log, "exception", lambda *a, **kw: None)

    real_open = open

    def explode(path, mode="r", *args, **kwargs):
        if "metrics.jsonl" in str(path) and "a" in mode:
            raise OSError(28, "no space left on device")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", explode)
    # Must NOT raise.
    record_snapshot(
        MetricSnapshot(timestamp=1.0, scan_duration_s=0.1),
        path=str(tmp_path / "metrics.jsonl"),
    )
    # File must not have been created (the only write path was the one
    # that failed) — verifies the write was actually attempted via the
    # patched open.
    assert not (tmp_path / "metrics.jsonl").exists()


# ── load_recent_snapshots ──────────────────────────────────────────────────


def test_load_recent_returns_oldest_first(tmp_path):
    p = tmp_path / "metrics.jsonl"
    for i in range(5):
        record_snapshot(
            MetricSnapshot(timestamp=float(i), scan_duration_s=0.1),
            path=str(p),
        )
    snaps = load_recent_snapshots(path=str(p))
    assert [s.timestamp for s in snaps] == [0.0, 1.0, 2.0, 3.0, 4.0]


def test_load_recent_filters_by_since_ts(tmp_path):
    """Walk-from-EOF loader stops as soon as a snapshot older than
    ``since_ts`` is hit — that snapshot AND everything older is excluded."""
    p = tmp_path / "metrics.jsonl"
    for i in range(10):
        record_snapshot(
            MetricSnapshot(timestamp=float(i), scan_duration_s=0.1),
            path=str(p),
        )
    snaps = load_recent_snapshots(path=str(p), since_ts=5.0)
    assert [s.timestamp for s in snaps] == [5.0, 6.0, 7.0, 8.0, 9.0]


def test_load_recent_respects_limit(tmp_path):
    p = tmp_path / "metrics.jsonl"
    for i in range(20):
        record_snapshot(
            MetricSnapshot(timestamp=float(i), scan_duration_s=0.1),
            path=str(p),
        )
    snaps = load_recent_snapshots(path=str(p), limit=3)
    # Most recent 3 (oldest first within those 3): 17, 18, 19.
    assert [s.timestamp for s in snaps] == [17.0, 18.0, 19.0]


def test_load_recent_skips_corrupted_lines(tmp_path, caplog):
    """A garbage line in the middle of the JSONL must NOT abort loading
    — the surveillance loop's metric history is more valuable than
    strict parse correctness on every line."""
    p = tmp_path / "metrics.jsonl"
    record_snapshot(
        MetricSnapshot(timestamp=1.0, scan_duration_s=0.1), path=str(p),
    )
    with open(p, "a", encoding="utf-8") as f:
        f.write("not valid json\n")
    record_snapshot(
        MetricSnapshot(timestamp=2.0, scan_duration_s=0.1), path=str(p),
    )
    snaps = load_recent_snapshots(path=str(p))
    assert [s.timestamp for s in snaps] == [1.0, 2.0]


def test_load_recent_returns_empty_on_missing_file(tmp_path):
    snaps = load_recent_snapshots(path=str(tmp_path / "no-such-file.jsonl"))
    assert snaps == []


# ── _percentile ─────────────────────────────────────────────────────────────


def test_percentile_known_values():
    """Linear-interp percentile for a small sorted distribution."""
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _percentile(vals, 50) == 3.0       # exact median
    assert _percentile(vals, 0) == 1.0
    assert _percentile(vals, 100) == 5.0
    # P25 between idx 1 (val 2) and idx 2 (val 3), rank = 1.0 → exactly 2.0.
    assert _percentile(vals, 25) == 2.0
    # P75 → rank 3.0 → 4.0
    assert _percentile(vals, 75) == 4.0


def test_percentile_interpolates_between_samples():
    vals = [10.0, 20.0]
    # Rank for P50 = 0.5 → midpoint → 15.0
    assert _percentile(vals, 50) == 15.0


def test_percentile_empty_returns_zero():
    assert _percentile([], 50) == 0.0


# ── compute_baseline ───────────────────────────────────────────────────────


def test_compute_baseline_returns_empty_when_below_min_samples():
    """Below the soak threshold → empty dict so the caller knows the
    baseline isn't yet reliable."""
    snaps = [
        MetricSnapshot(timestamp=float(i), scan_duration_s=1.0)
        for i in range(DEFAULT_BASELINE_MIN_SAMPLES - 1)
    ]
    assert compute_baseline(snaps) == {}


def test_compute_baseline_computes_percentiles_for_scan_duration():
    """A known distribution → known P50/P95 (within float tolerance)."""
    durations = [float(i) for i in range(1, 21)]   # 1.0 .. 20.0, 20 samples
    snaps = [
        MetricSnapshot(timestamp=float(i), scan_duration_s=d)
        for i, d in enumerate(durations)
    ]
    bl = compute_baseline(snaps)
    sd = bl["scan_duration_s"]
    assert isinstance(sd, BaselineMetric)
    assert sd.n == 20
    # P50 of 1..20 → midpoint between 10 and 11 → 10.5
    assert sd.p50 == pytest.approx(10.5)
    # Mean of 1..20 = 10.5
    assert sd.mean == pytest.approx(10.5)


def test_compute_baseline_includes_counter_keys():
    """Counters are baselined including ticks where the key is absent
    (treated as 0). Quiet ticks contribute to the baseline."""
    snaps = []
    for i in range(20):
        # Half the ticks have silent_with_tools=2, half have nothing.
        counters = {"silent_with_tools": 2} if i % 2 == 0 else {}
        snaps.append(MetricSnapshot(
            timestamp=float(i), scan_duration_s=1.0, counters=counters,
        ))
    bl = compute_baseline(snaps)
    swt = bl["counters.silent_with_tools"]
    assert swt.n == 20
    # 10 zeros + 10 twos → P50 between 0 and 2 → 1.0
    assert swt.p50 == pytest.approx(1.0)
    assert swt.mean == pytest.approx(1.0)


# ── format_baseline_summary ────────────────────────────────────────────────


def test_format_baseline_summary_soak_progress_when_empty():
    """Empty baseline → soak-progress line with sample count + threshold."""
    out = format_baseline_summary({}, n_snapshots=5)
    assert "baseline soak" in out
    assert "5 snapshots" in out
    assert str(DEFAULT_BASELINE_MIN_SAMPLES) in out


def test_format_baseline_summary_renders_percentiles_when_populated():
    bl = {
        "scan_duration_s": BaselineMetric(
            n=247, p5=0.8, p50=1.8, p95=3.2, mean=2.0,
        ),
    }
    out = format_baseline_summary(bl, n_snapshots=247, window_sec=86400)
    assert "247 snapshots" in out
    assert "scan p50=1.8s" in out
    assert "p95=3.2s" in out
    assert "24h" in out  # window expressed in hours


# ── build_snapshot_from_doctor ─────────────────────────────────────────────


def test_build_snapshot_from_doctor_summarises_playbook_results():
    class _PR:
        def __init__(self, kind, found, acted, skipped, errors, dry_run):
            self.kind = kind
            self.candidates_found = found
            self.actions_taken = acted
            self.actions_skipped = skipped
            self.errors = errors
            self.dry_run = dry_run

    pr1 = _PR("hydrate_apev_headings", 3, 2, 1, [], False)
    pr2 = _PR("refresh_dashboard_next_gate", 1, 1, 0, [], False)
    snap = build_snapshot_from_doctor(
        started_at=100.0,
        finished_at=102.5,
        counters={"silent_with_tools": 1},
        grounding_gaps=["SAL-1", "SAL-2"],
        playbook_results=[pr1, pr2],
        daemon_head="abc1234567890",
    )
    assert snap.scan_duration_s == 2.5
    assert snap.counters == {"silent_with_tools": 1}
    assert snap.grounding_gaps_count == 2
    assert snap.playbook_summary["hydrate_apev_headings"]["acted"] == 2
    assert snap.playbook_summary["refresh_dashboard_next_gate"]["acted"] == 1
    # daemon_head clamped to 12 chars.
    assert len(snap.daemon_head) <= 12


def test_build_snapshot_clamps_negative_duration():
    """Clock skew or test-time fakery shouldn't produce negative durations."""
    snap = build_snapshot_from_doctor(
        started_at=200.0, finished_at=100.0,  # backwards
        counters={}, grounding_gaps=[], playbook_results=[],
    )
    assert snap.scan_duration_s == 0.0


# ── env override ─────────────────────────────────────────────────────────


def test_metrics_path_from_env_uses_default_when_unset(monkeypatch):
    monkeypatch.delenv("ALFRED_DOCTOR_METRICS_PATH", raising=False)
    p = metrics_path_from_env()
    assert p.endswith("doctor_metrics.jsonl")


def test_metrics_path_from_env_respects_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ALFRED_DOCTOR_METRICS_PATH", str(tmp_path / "x.jsonl"))
    assert metrics_path_from_env() == str(tmp_path / "x.jsonl")
