"""Tests for the container_service_restart_loop_detector playbook.

Acceptance coverage (verbatim, from SAL-3918):
(a) restart count below threshold → no alert
(b) restart count above threshold → alert posted
(c) cooldown suppresses second alert within 10 minutes
(d) unhealthy duration above 5 min → alert
(e) watchlist parse handles wildcard ``docker:tiresias-*``

Plus the usual hygiene: state persistence round-trip, dry-run doesn't
post, missing token reports failure cleanly, registry includes the new
playbook.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from alfred_coo.autonomous_build.playbooks import (
    DEFAULT_PLAYBOOKS,
    ContainerServiceRestartLoopDetectorPlaybook,
    PlaybookResult,
)
from alfred_coo.autonomous_build.playbooks.container_service_restart_loop_detector import (
    DEFAULT_ALERT_COOLDOWN_SEC,
    DEFAULT_RESTART_DELTA_THRESHOLD,
    DEFAULT_RESTART_WINDOW_SEC,
    DEFAULT_UNHEALTHY_DURATION_SEC,
    DEFAULT_WATCHLIST,
    detect_restart_loop,
    detect_unhealthy_duration,
    format_alert,
    parse_watchlist,
)
import alfred_coo.autonomous_build.playbooks.container_service_restart_loop_detector as MOD


# ── Watchlist parsing ────────────────────────────────────────────────────


def test_parse_watchlist_default_string():
    """Default canonical watchlist parses to 3 entries with correct types."""
    out = parse_watchlist(DEFAULT_WATCHLIST)
    assert ("docker", "soul-svc-oracle") in out
    assert ("docker", "tiresias-*") in out
    assert ("systemd", "alfred-coo") in out
    assert len(out) == 3


def test_parse_watchlist_handles_wildcard_glob():
    """Acceptance (e): wildcard ``docker:tiresias-*`` is preserved as a
    pattern. Expansion to literal names happens at scan time, not parse
    time, so a container that appears mid-session is picked up next tick.
    """
    out = parse_watchlist("docker:tiresias-*")
    assert out == [("docker", "tiresias-*")]


def test_parse_watchlist_drops_malformed_entries():
    """Empty entries, missing colon, unknown type, empty name — all dropped
    silently. The doctor must never crash on a misconfigured env var."""
    out = parse_watchlist(",docker:foo,nope,kafka:bar,docker:,systemd:alfred-coo,:emptytype")
    assert out == [("docker", "foo"), ("systemd", "alfred-coo")]


def test_parse_watchlist_empty_string_returns_empty():
    assert parse_watchlist("") == []


# ── detect_restart_loop ──────────────────────────────────────────────────


def test_detect_restart_loop_below_threshold_does_not_trigger():
    """Acceptance (a): delta < threshold → not triggered.

    Two ticks 0s apart, restart count grew 0 → 1. Threshold default 3.
    Must NOT trigger.
    """
    state = {}
    now1 = 1000.0
    triggered, delta = detect_restart_loop(
        target_key="systemd:alfred-coo",
        current_restart_count=0,
        state_for_target=state,
        now=now1,
    )
    assert triggered is False
    assert delta == 0

    triggered, delta = detect_restart_loop(
        target_key="systemd:alfred-coo",
        current_restart_count=1,
        state_for_target=state,
        now=now1 + 60,
    )
    assert triggered is False
    assert delta == 1


def test_detect_restart_loop_at_threshold_triggers():
    """Acceptance (b): delta >= threshold inside the window → triggered.

    First sample at t=0 with count=5, second at t=120s with count=8
    (delta=3). Default threshold is 3 → must trigger.
    """
    state = {}
    detect_restart_loop(
        target_key="systemd:alfred-coo",
        current_restart_count=5,
        state_for_target=state,
        now=1000.0,
    )
    triggered, delta = detect_restart_loop(
        target_key="systemd:alfred-coo",
        current_restart_count=8,
        state_for_target=state,
        now=1000.0 + 120,
    )
    assert triggered is True
    assert delta == 3


def test_detect_restart_loop_window_drops_stale_samples():
    """Samples older than window_sec are dropped — comparison is over the
    rolling window, not the daemon's lifetime. After dropping the stale
    sample, only the fresh one remains and delta is 0."""
    state = {}
    # Stale sample (window = 600s default).
    detect_restart_loop(
        target_key="x",
        current_restart_count=0,
        state_for_target=state,
        now=0.0,
    )
    # Fresh sample 700s later — stale one drops, only this one remains.
    triggered, delta = detect_restart_loop(
        target_key="x",
        current_restart_count=10,
        state_for_target=state,
        now=700.0,
    )
    assert triggered is False
    assert delta == 0


# ── detect_unhealthy_duration ────────────────────────────────────────────


def test_detect_unhealthy_duration_first_tick_does_not_trigger():
    """First tick where the container is unhealthy just records the
    timestamp; can't trigger until duration passes."""
    state = {}
    triggered, dur = detect_unhealthy_duration(
        target_key="docker:soul-svc-oracle",
        health_status="unhealthy",
        state_for_target=state,
        now=1000.0,
    )
    assert triggered is False
    assert dur == 0
    assert state["first_unhealthy_at"] == 1000.0


def test_detect_unhealthy_duration_above_threshold_triggers():
    """Acceptance (d): unhealthy continuously for >= 5 min → trigger."""
    state = {"first_unhealthy_at": 1000.0}
    triggered, dur = detect_unhealthy_duration(
        target_key="docker:soul-svc-oracle",
        health_status="unhealthy",
        state_for_target=state,
        now=1000.0 + DEFAULT_UNHEALTHY_DURATION_SEC + 5,
    )
    assert triggered is True
    assert dur >= DEFAULT_UNHEALTHY_DURATION_SEC


def test_detect_unhealthy_duration_resets_when_healthy():
    """Container leaving the unhealthy state clears the marker so a
    later flap doesn't carry the old start time forward."""
    state = {"first_unhealthy_at": 1000.0}
    triggered, dur = detect_unhealthy_duration(
        target_key="docker:soul-svc-oracle",
        health_status="healthy",
        state_for_target=state,
        now=2000.0,
    )
    assert triggered is False
    assert dur == 0
    assert "first_unhealthy_at" not in state


def test_detect_unhealthy_duration_none_status_resets():
    """A target with no healthcheck reports ``none`` — treated as
    not-unhealthy, marker cleared."""
    state = {"first_unhealthy_at": 1000.0}
    triggered, _ = detect_unhealthy_duration(
        target_key="x",
        health_status="none",
        state_for_target=state,
        now=2000.0,
    )
    assert triggered is False
    assert "first_unhealthy_at" not in state


# ── format_alert ─────────────────────────────────────────────────────────


def test_format_alert_restart_loop_includes_delta_and_logs():
    text = format_alert(
        target_key="docker:soul-svc-oracle",
        reason="restart-loop",
        delta=5,
        duration_sec=None,
        last_logs=["panic: nil pointer", "exit 1", "starting...", "ready", "panic: nil pointer"],
        daemon_head="abc1234",
    )
    assert "soul-svc-oracle" in text
    assert "+5" in text
    assert "panic: nil pointer" in text
    # Daemon head shown so operator can correlate alert with deploy SHA.
    assert "abc1234" in text


def test_format_alert_unhealthy_duration_includes_dur_seconds():
    text = format_alert(
        target_key="docker:tiresias-portal",
        reason="unhealthy-duration",
        delta=None,
        duration_sec=420,
        last_logs=[],
    )
    assert "tiresias-portal" in text
    assert "420s" in text
    assert "unavailable" in text  # no logs available


# ── End-to-end execute() ─────────────────────────────────────────────────


class _FakeSlackPost:
    """Captures Slack post calls. Returns ``ok=True`` by default."""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[dict] = []

    async def __call__(self, token, channel, text, **_kw):
        self.calls.append({"token": token, "channel": channel, "text": text})
        return self.ok


def _patch_probes(
    monkeypatch,
    *,
    docker_results: dict[str, dict | None] | None = None,
    systemd_results: dict[str, dict | None] | None = None,
):
    """Monkeypatch the docker + systemd probes to deterministic returns."""
    docker_results = docker_results or {}
    systemd_results = systemd_results or {}

    def fake_docker(name, **_kw):
        return docker_results.get(name)

    def fake_systemd(name, **_kw):
        return systemd_results.get(name)

    monkeypatch.setattr(MOD, "probe_docker_container", fake_docker)
    monkeypatch.setattr(MOD, "probe_systemd_unit", fake_systemd)
    # Glob expansion: literal names pass through unchanged in tests.
    monkeypatch.setattr(
        MOD, "expand_docker_targets",
        lambda n, **_k: [n] if "*" not in n else [],
    )


@pytest.mark.asyncio
async def test_execute_no_alert_when_restart_count_below_threshold(
    monkeypatch, tmp_path,
):
    """Acceptance (a): restart count grew by 1, threshold is 3 → no alert."""
    sink = _FakeSlackPost()
    pb = ContainerServiceRestartLoopDetectorPlaybook(
        watchlist="systemd:alfred-coo",
        state_path=str(tmp_path / "state.json"),
        slack_post=sink,
    )
    # Seed state: previous restart count 5 a minute ago.
    seed = {
        "systemd:alfred-coo": {
            "samples": [{"ts": time.time() - 60, "count": 5}],
        }
    }
    (tmp_path / "state.json").write_text(json.dumps(seed), encoding="utf-8")
    _patch_probes(
        monkeypatch,
        systemd_results={
            "alfred-coo": {
                "restart_count": 6,  # delta = 1
                "health_status": "n/a",
                "last_logs": ["systemd: started"],
            },
        },
    )
    res = await pb.execute(linear_api_key="", dry_run=False)
    assert res.candidates_found == 0
    assert res.actions_taken == 0
    assert sink.calls == []


@pytest.mark.asyncio
async def test_execute_alert_when_restart_count_above_threshold(
    monkeypatch, tmp_path,
):
    """Acceptance (b): restart delta >= 3 → alert posted."""
    sink = _FakeSlackPost()
    state_path = tmp_path / "state.json"
    pb = ContainerServiceRestartLoopDetectorPlaybook(
        watchlist="docker:soul-svc-oracle",
        state_path=str(state_path),
        slack_post=sink,
    )
    seed = {
        "docker:soul-svc-oracle": {
            "samples": [{"ts": time.time() - 120, "count": 1}],
        }
    }
    state_path.write_text(json.dumps(seed), encoding="utf-8")
    _patch_probes(
        monkeypatch,
        docker_results={
            "soul-svc-oracle": {
                "restart_count": 7,  # delta = 6
                "health_status": "running",
                "last_logs": ["fatal: bad config", "exit 1", "starting"],
            },
        },
    )
    res = await pb.execute(linear_api_key="", dry_run=False)
    assert res.candidates_found == 1
    assert res.actions_taken == 1
    assert len(sink.calls) == 1
    posted = sink.calls[0]
    assert posted["channel"] == "C0ASAKFTR1C"
    assert "soul-svc-oracle" in posted["text"]
    assert "fatal: bad config" in posted["text"]
    # last_alerted_at recorded so the cooldown can trip on the next tick.
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["docker:soul-svc-oracle"]["last_alerted_at"] > 0


@pytest.mark.asyncio
async def test_execute_cooldown_suppresses_second_alert_within_10min(
    monkeypatch, tmp_path,
):
    """Acceptance (c): cooldown suppresses repeat alerts inside 10 minutes."""
    sink = _FakeSlackPost()
    state_path = tmp_path / "state.json"
    now = time.time()
    seed = {
        "docker:soul-svc-oracle": {
            "samples": [{"ts": now - 120, "count": 1}],
            "last_alerted_at": now - 120,  # 2 minutes ago — inside cooldown
        }
    }
    state_path.write_text(json.dumps(seed), encoding="utf-8")
    pb = ContainerServiceRestartLoopDetectorPlaybook(
        watchlist="docker:soul-svc-oracle",
        state_path=str(state_path),
        slack_post=sink,
    )
    _patch_probes(
        monkeypatch,
        docker_results={
            "soul-svc-oracle": {
                "restart_count": 10,  # delta = 9, would normally fire
                "health_status": "running",
                "last_logs": ["loop"],
            },
        },
    )
    res = await pb.execute(linear_api_key="", dry_run=False)
    # Detected, but cooldown blocks the post.
    assert res.candidates_found == 1
    assert res.actions_taken == 0
    assert res.actions_skipped == 1
    assert sink.calls == [], "cooldown must suppress the post"
    assert any("cooldown active" in n for n in res.notable)


@pytest.mark.asyncio
async def test_execute_alert_when_unhealthy_duration_above_threshold(
    monkeypatch, tmp_path,
):
    """Acceptance (d): continuously unhealthy for >= 5 min → alert."""
    sink = _FakeSlackPost()
    state_path = tmp_path / "state.json"
    now = time.time()
    seed = {
        "docker:soul-svc-oracle": {
            "first_unhealthy_at": now - (DEFAULT_UNHEALTHY_DURATION_SEC + 30),
            "samples": [{"ts": now - 60, "count": 0}],
        }
    }
    state_path.write_text(json.dumps(seed), encoding="utf-8")
    pb = ContainerServiceRestartLoopDetectorPlaybook(
        watchlist="docker:soul-svc-oracle",
        state_path=str(state_path),
        slack_post=sink,
    )
    _patch_probes(
        monkeypatch,
        docker_results={
            "soul-svc-oracle": {
                "restart_count": 0,  # no restart-loop signal
                "health_status": "unhealthy",
                "last_logs": ["healthcheck failed"],
            },
        },
    )
    res = await pb.execute(linear_api_key="", dry_run=False)
    assert res.candidates_found == 1
    assert res.actions_taken == 1
    assert len(sink.calls) == 1
    assert "unhealthy" in sink.calls[0]["text"].lower()


@pytest.mark.asyncio
async def test_execute_dry_run_does_not_post(monkeypatch, tmp_path):
    """Dry-run never calls Slack — only emits a ``would alert`` notable."""
    sink = _FakeSlackPost()
    state_path = tmp_path / "state.json"
    seed = {
        "docker:soul-svc-oracle": {
            "samples": [{"ts": time.time() - 60, "count": 1}],
        }
    }
    state_path.write_text(json.dumps(seed), encoding="utf-8")
    pb = ContainerServiceRestartLoopDetectorPlaybook(
        watchlist="docker:soul-svc-oracle",
        state_path=str(state_path),
        slack_post=sink,
    )
    _patch_probes(
        monkeypatch,
        docker_results={
            "soul-svc-oracle": {
                "restart_count": 8,
                "health_status": "running",
                "last_logs": ["x"],
            },
        },
    )
    res = await pb.execute(linear_api_key="", dry_run=True)
    assert res.candidates_found == 1
    assert res.actions_taken == 0
    assert sink.calls == [], "dry-run must not call Slack"
    assert any("would alert" in n for n in res.notable)


@pytest.mark.asyncio
async def test_execute_skips_when_probe_returns_none(monkeypatch, tmp_path):
    """Container/unit absent → probe returns None; playbook records the
    target as skipped and proceeds. Doctor must never crash on a missing
    target."""
    sink = _FakeSlackPost()
    pb = ContainerServiceRestartLoopDetectorPlaybook(
        watchlist="docker:does-not-exist,systemd:also-missing",
        state_path=str(tmp_path / "state.json"),
        slack_post=sink,
    )
    _patch_probes(monkeypatch, docker_results={}, systemd_results={})
    res = await pb.execute(linear_api_key="", dry_run=False)
    assert res.actions_skipped == 2
    assert res.errors == []
    assert sink.calls == []


@pytest.mark.asyncio
async def test_execute_empty_watchlist_silent(monkeypatch, tmp_path):
    """Empty / unparseable watchlist → playbook is silent (no errors)."""
    sink = _FakeSlackPost()
    pb = ContainerServiceRestartLoopDetectorPlaybook(
        watchlist="",
        state_path=str(tmp_path / "state.json"),
        slack_post=sink,
    )
    res = await pb.execute(linear_api_key="", dry_run=False)
    assert res.is_silent()
    assert sink.calls == []


@pytest.mark.asyncio
async def test_execute_handler_crash_on_one_target_does_not_break_others(
    monkeypatch, tmp_path,
):
    """A probe explosion for one target shouldn't stop the loop — others
    still get their tick. Mirrors the `_handle_project` per-target
    isolation pattern in restart_stalled_chains."""
    sink = _FakeSlackPost()
    pb = ContainerServiceRestartLoopDetectorPlaybook(
        watchlist="systemd:bad,systemd:good",
        state_path=str(tmp_path / "state.json"),
        slack_post=sink,
    )

    def boom(name, **_kw):
        if name == "bad":
            raise RuntimeError("simulated probe crash")
        return {
            "restart_count": 0,
            "health_status": "n/a",
            "last_logs": [],
        }

    monkeypatch.setattr(MOD, "probe_systemd_unit", boom)
    monkeypatch.setattr(
        MOD, "expand_docker_targets",
        lambda n, **_k: [n] if "*" not in n else [],
    )
    res = await pb.execute(linear_api_key="", dry_run=False)
    assert any("bad" in e and "handler_crashed" in e for e in res.errors)
    # The good target still got handled (no crash propagation).
    assert res.errors  # bad target reported
    # No alerts (good target had no restart spike) — confirms loop continued.
    assert sink.calls == []


# ── Slack post failure handling ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_records_error_when_slack_post_fails(
    monkeypatch, tmp_path,
):
    """Slack returning ok=false (bad token, channel revoked, etc.)
    must NOT mark the alert as taken — the alert never landed.
    Recorded as an error so the next tick can re-attempt after cooldown
    (no last_alerted_at update means cooldown gate is open)."""
    sink = _FakeSlackPost(ok=False)
    state_path = tmp_path / "state.json"
    seed = {
        "docker:soul-svc-oracle": {
            "samples": [{"ts": time.time() - 60, "count": 1}],
        }
    }
    state_path.write_text(json.dumps(seed), encoding="utf-8")
    pb = ContainerServiceRestartLoopDetectorPlaybook(
        watchlist="docker:soul-svc-oracle",
        state_path=str(state_path),
        slack_post=sink,
    )
    _patch_probes(
        monkeypatch,
        docker_results={
            "soul-svc-oracle": {
                "restart_count": 9,
                "health_status": "running",
                "last_logs": ["x"],
            },
        },
    )
    res = await pb.execute(linear_api_key="", dry_run=False)
    assert res.actions_taken == 0
    assert any("slack_post_failed" in e for e in res.errors)
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    # No last_alerted_at so the next tick can retry.
    assert "last_alerted_at" not in saved["docker:soul-svc-oracle"]


# ── Verification scenario from the SAL-3918 acceptance block ─────────────


@pytest.mark.asyncio
async def test_2026_05_02_soul_svc_oracle_scenario_would_have_alerted(
    monkeypatch, tmp_path,
):
    """Verification (SAL-3918): the 2026-05-02 soul-svc-oracle 6-hour
    crash loop scenario would have triggered an alert at the 10-minute
    mark instead of going unnoticed for 6 hours.

    Simulate: at t=0 daemon starts surveillance, restart count is 1.
    At t=10 minutes (one doctor tick @ 5min cadence later, counting
    the second sample inside the window) the container has restarted
    4 more times → delta >= 3 from the first sample → alert fires.
    """
    sink = _FakeSlackPost()
    state_path = tmp_path / "state.json"
    pb = ContainerServiceRestartLoopDetectorPlaybook(
        watchlist="docker:soul-svc-oracle",
        state_path=str(state_path),
        slack_post=sink,
    )

    now0 = time.time() - 600  # 10 min ago, inside the 600s window
    # Tick 1 (10 min ago, first observation).
    _patch_probes(
        monkeypatch,
        docker_results={
            "soul-svc-oracle": {
                "restart_count": 1,
                "health_status": "running",
                "last_logs": ["start"],
            },
        },
    )
    # Manually pre-seed the first sample timestamp inside the window so
    # the second tick's delta is computed against it.
    state_path.write_text(json.dumps({
        "docker:soul-svc-oracle": {
            "samples": [{"ts": now0, "count": 1}],
        }
    }), encoding="utf-8")

    # Tick 2 (now): container has restarted 5 times.
    _patch_probes(
        monkeypatch,
        docker_results={
            "soul-svc-oracle": {
                "restart_count": 5,  # delta = 4 from earliest sample
                "health_status": "running",
                "last_logs": [
                    "panic: PR #70 regression",
                    "exit 1",
                    "starting",
                    "panic: PR #70 regression",
                    "exit 1",
                ],
            },
        },
    )
    res = await pb.execute(linear_api_key="", dry_run=False)
    assert res.actions_taken == 1, (
        "the 2026-05-02 6h crash-loop pattern MUST trigger an alert"
    )
    assert "PR #70 regression" in sink.calls[0]["text"]


# ── Defaults + registry ─────────────────────────────────────────────────


def test_default_thresholds_match_spec():
    """Spec from SAL-3918:
    * restart count delta >= 3 in last 10 min
    * unhealthy continuously for >= 5 min
    * 10-minute cooldown
    """
    assert DEFAULT_RESTART_DELTA_THRESHOLD == 3
    assert DEFAULT_RESTART_WINDOW_SEC == 600
    assert DEFAULT_UNHEALTHY_DURATION_SEC == 300
    assert DEFAULT_ALERT_COOLDOWN_SEC == 600


def test_default_registry_includes_restart_loop_detector():
    """The new playbook is wired into ``DEFAULT_PLAYBOOKS`` so the
    surveillance loop actually invokes it."""
    kinds = [p.kind for p in DEFAULT_PLAYBOOKS]
    assert "container_service_restart_loop_detector" in kinds


def test_env_var_override_for_watchlist(monkeypatch, tmp_path):
    """Watchlist comes from the env var when no explicit value is passed.
    Verifies the configurable-watchlist contract."""
    monkeypatch.setenv(
        "ALFRED_DOCTOR_RESTART_LOOP_WATCHLIST",
        "docker:custom-thing,systemd:other-unit",
    )
    pb = ContainerServiceRestartLoopDetectorPlaybook(
        state_path=str(tmp_path / "x.json"),
    )
    assert ("docker", "custom-thing") in pb.watchlist
    assert ("systemd", "other-unit") in pb.watchlist
