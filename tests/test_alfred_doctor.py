"""Tests for alfred-doctor Phase 1 (surveillance + Slack reporting).

Covers:
* persona registry entry exists with the right handler name
* payload parsing (interval clamps, channel default, last_scan_ts floor)
* failure-mode classification helper
* grounding-gap ident extraction from result envelopes
* mesh recent-task scan filters by since_ts and counts modes
* journal scan counts known patterns and is robust to missing binary
* Slack message formatter produces a quiet-vs-noisy digest
* end-to-end run() queues the next kickoff with updated last_scan_ts
"""

from __future__ import annotations

import json
import time
import types

import pytest

from alfred_coo.autonomous_build.doctor import (
    AlfredDoctorOrchestrator,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_SLACK_CHANNEL,
    ScanReport,
    _classify_failure_mode,
    _extract_grounding_gap_idents,
    _parse_interval_seconds,
    _parse_slack_channel,
    format_slack_message,
    scan_journal_via_subprocess,
    scan_mesh_recent_tasks,
)
from alfred_coo.autonomous_build.playbooks import PlaybookResult
from alfred_coo.persona import get_persona


# ── Persona registration ────────────────────────────────────────────────────


def test_alfred_doctor_persona_registered():
    """The persona must exist with handler=AlfredDoctorOrchestrator so
    main.py's _resolve_handler can find the class on claim."""
    p = get_persona("alfred-doctor")
    assert p.name == "alfred-doctor"
    assert p.handler == "AlfredDoctorOrchestrator"
    # No tool-use loop expected (long-running orchestrator path).
    assert p.tools == []


# ── Payload parsing ─────────────────────────────────────────────────────────


def test_parse_interval_seconds_default_when_missing():
    assert _parse_interval_seconds({}) == DEFAULT_INTERVAL_SECONDS


def test_parse_interval_seconds_accepts_override():
    assert _parse_interval_seconds({"interval_seconds": 120}) == 120


def test_parse_interval_seconds_clamps_low_and_high():
    """A misconfigured payload (e.g., 0 or 99999) must clamp to [60, 3600]
    so a runaway doctor can't tight-loop or sleep through a whole shift."""
    assert _parse_interval_seconds({"interval_seconds": 0}) == 60
    assert _parse_interval_seconds({"interval_seconds": 30}) == 60
    assert _parse_interval_seconds({"interval_seconds": 99999}) == 3600


def test_parse_interval_seconds_falls_back_on_garbage():
    assert _parse_interval_seconds({"interval_seconds": "abc"}) == DEFAULT_INTERVAL_SECONDS


def test_parse_slack_channel_default_and_override():
    assert _parse_slack_channel({}) == DEFAULT_SLACK_CHANNEL
    assert _parse_slack_channel({"slack_channel": "C12345"}) == "C12345"
    # Empty string falls back to default rather than posting nowhere.
    assert _parse_slack_channel({"slack_channel": "  "}) == DEFAULT_SLACK_CHANNEL


# ── Failure-mode classification ─────────────────────────────────────────────


def test_classify_failure_mode_silent_with_tools():
    result = {"silent_with_tools": True}
    assert _classify_failure_mode(result, "completed") == "silent_with_tools"


def test_classify_failure_mode_grounding_gap():
    result = {"summary": "Escalated SAL-3544 as grounding gap due to ..."}
    assert _classify_failure_mode(result, "completed") == "grounding_gap_escalation"


def test_classify_failure_mode_hard_timeout_failed():
    result = {"summary": "builder hard-timeout: dispatched 600s ago"}
    assert _classify_failure_mode(result, "failed") == "hard_timeout"


def test_classify_failure_mode_other_failed_when_no_signal():
    assert _classify_failure_mode({"summary": ""}, "failed") == "other_failed"


def test_classify_failure_mode_other_completed_when_no_signal():
    assert _classify_failure_mode({"summary": ""}, "completed") == "other_completed"


# ── Grounding-gap ident extraction ──────────────────────────────────────────


def test_extract_grounding_gap_idents_from_summary():
    result = {"summary": "Escalated SAL-3614 to Linear issue SAL-3823"}
    assert _extract_grounding_gap_idents(result) == ["SAL-3614", "SAL-3823"]


def test_extract_grounding_gap_idents_from_tool_calls():
    result = {
        "summary": "fine summary",
        "tool_calls": [
            {
                "name": "linear_create_issue",
                "arguments": json.dumps({"title": "Grounding gap: SAL-3548 missing plan doc"}),
                "result": json.dumps({"identifier": "SAL-3820"}),
            },
        ],
    }
    assert _extract_grounding_gap_idents(result) == ["SAL-3820"]


def test_extract_grounding_gap_idents_dedupes():
    result = {
        "summary": "SAL-3823 SAL-3823 grounding gap",
        "tool_calls": [
            {
                "name": "linear_create_issue",
                "arguments": json.dumps({"title": "grounding gap: SAL-3614"}),
                "result": json.dumps({"identifier": "SAL-3823"}),
            }
        ],
    }
    out = _extract_grounding_gap_idents(result)
    # No duplicates, order preserved.
    assert out == ["SAL-3823"]


def test_extract_grounding_gap_idents_ignores_non_gap_tool_calls():
    result = {
        "summary": "ok",
        "tool_calls": [
            {"name": "propose_pr", "arguments": "{}", "result": "{}"},
            {
                "name": "linear_create_issue",
                "arguments": json.dumps({"title": "follow-up: refactor X"}),
                "result": json.dumps({"identifier": "SAL-9999"}),
            },
        ],
    }
    assert _extract_grounding_gap_idents(result) == []


# ── Mesh recent-task scan ───────────────────────────────────────────────────


class _FakeMesh:
    def __init__(self, failed=None, completed=None):
        self._failed = failed or []
        self._completed = completed or []

    async def list_tasks(self, *, status=None, limit=50):
        if status == "failed":
            return list(self._failed)
        if status == "completed":
            return list(self._completed)
        return []


@pytest.mark.asyncio
async def test_scan_mesh_recent_tasks_filters_by_since_ts():
    """Tasks completed before since_ts must NOT be counted."""
    old_iso = "2026-04-30T00:00:00+00:00"
    new_iso = "2026-05-01T20:00:00+00:00"
    mesh = _FakeMesh(
        completed=[
            {
                "title": "[builder] SAL-1111",
                "completed_at": old_iso,
                "result": {"summary": "shipped"},
            },
            {
                "title": "[builder] SAL-2222",
                "completed_at": new_iso,
                "result": {"summary": "shipped"},
            },
        ],
    )
    # since_ts mid-2026-05-01 in unix seconds → only SAL-2222 counts.
    since_ts = time.mktime(time.strptime("2026-05-01 00:00:00", "%Y-%m-%d %H:%M:%S"))
    report = ScanReport(started_at=time.time())
    await scan_mesh_recent_tasks(mesh, since_ts=since_ts, report=report)
    assert report.counters.get("other_completed", 0) == 1


@pytest.mark.asyncio
async def test_scan_mesh_recent_tasks_classifies_modes_and_collects_gaps():
    """A grounding-gap-escalation result should both bump the counter AND
    surface the cited Linear identifier in report.grounding_gaps."""
    new_iso = "2026-05-01T20:00:00+00:00"
    mesh = _FakeMesh(
        completed=[
            {
                "title": "[builder] SAL-3544 MSSP-EX-G",
                "completed_at": new_iso,
                "result": {
                    "summary": "Escalated SAL-3544 as grounding gap. Created Linear issue SAL-3819.",
                    "tool_calls": [
                        {
                            "name": "linear_create_issue",
                            "arguments": json.dumps({"title": "grounding gap: SAL-3544 missing plan doc"}),
                            "result": json.dumps({"identifier": "SAL-3819"}),
                        }
                    ],
                },
            },
        ],
    )
    since_ts = time.mktime(time.strptime("2026-05-01 00:00:00", "%Y-%m-%d %H:%M:%S"))
    report = ScanReport(started_at=time.time())
    await scan_mesh_recent_tasks(mesh, since_ts=since_ts, report=report)
    assert report.counters.get("grounding_gap_escalation", 0) == 1
    assert "SAL-3819" in report.grounding_gaps


# ── Journal scan ────────────────────────────────────────────────────────────


def test_scan_journal_handles_missing_binary_gracefully(tmp_path):
    """If the host has no journalctl, the doctor must record a counter
    and continue, NOT raise. This is the test-env path — surveillance
    should be best-effort."""
    report = ScanReport(started_at=time.time())
    scan_journal_via_subprocess(
        lookback_seconds=60,
        report=report,
        binary=str(tmp_path / "no-such-binary"),
    )
    assert report.counters.get("journal_unavailable", 0) == 1


def test_scan_journal_counts_patterns(monkeypatch, tmp_path):
    """When journalctl returns content, the scanner counts each known
    pattern and samples one matching line per pattern."""
    sample_output = (
        "line a\n"
        "silent_with_tools detected: 'http_get' called 3 iter\n"
        "another line\n"
        "silent_with_tools detected: 'http_get' called 3 iter\n"
        "[wave-retry] queued fresh kickoff abc-123 for wave=2\n"
        "decision=failed_below_threshold ratio=0.00\n"
        "builder hard-timeout: SAL-3614 dispatched 609s\n"
        "[infra_retry] http://gateway attempt=2/3\n"
    )
    fake_proc = types.SimpleNamespace(stdout=sample_output, returncode=0)

    def fake_run(*args, **kwargs):
        return fake_proc

    monkeypatch.setattr("alfred_coo.autonomous_build.doctor.subprocess.run", fake_run)
    report = ScanReport(started_at=time.time())
    scan_journal_via_subprocess(lookback_seconds=60, report=report)
    assert report.counters["journal_silent_with_tools"] == 2
    assert report.counters["journal_wave_retry_fired"] == 1
    assert report.counters["journal_wave_gate_failed"] == 1
    assert report.counters["journal_hard_timeout"] == 1
    assert report.counters["journal_infra_retry"] == 1
    # Sample lines populated for the digest.
    assert len(report.notable_lines) >= 4


# ── Slack message formatting ────────────────────────────────────────────────


def test_format_slack_message_quiet_window():
    report = ScanReport(started_at=time.time())
    report.finished_at = report.started_at + 0.5
    msg = format_slack_message(report, daemon_head="abcdefg")
    assert "Substrate quiet" in msg
    assert "abcdefg" in msg


def test_format_slack_message_noisy_window():
    report = ScanReport(started_at=time.time())
    report.finished_at = report.started_at + 1.2
    report.add("silent_with_tools", count=4)
    report.add("journal_wave_retry_fired", count=1)
    report.grounding_gaps.extend(["SAL-3819", "SAL-3823"])
    msg = format_slack_message(report)
    assert "silent_with_tools: 4" in msg
    assert "journal_wave_retry_fired: 1" in msg
    assert "SAL-3819" in msg and "SAL-3823" in msg


def test_format_slack_message_includes_playbook_results():
    """Playbook results render under a ``playbooks:`` header so a quiet
    surveillance scan with active playbook output still emits a digest."""
    report = ScanReport(started_at=time.time())
    report.finished_at = report.started_at + 0.7
    pr = PlaybookResult(
        kind="hydrate_apev_headings",
        candidates_found=3,
        actions_taken=0,
        dry_run=True,
        notable=["would hydrate SAL-3001", "would hydrate SAL-3002"],
    )
    msg = format_slack_message(report, playbook_results=[pr])
    assert "playbooks:" in msg
    assert "[dry] hydrate_apev_headings" in msg
    assert "found=3" in msg
    assert "would hydrate SAL-3001" in msg


def test_format_slack_message_silent_playbooks_collapse_to_quiet():
    """When playbooks have nothing to report and surveillance is quiet,
    digest collapses to ``Substrate quiet.`` — no noise from empty
    playbook lines."""
    report = ScanReport(started_at=time.time())
    report.finished_at = report.started_at + 0.5
    pr = PlaybookResult(kind="hydrate_apev_headings", dry_run=True)
    msg = format_slack_message(report, playbook_results=[pr])
    assert "Substrate quiet" in msg
    assert "playbooks:" not in msg


# ── End-to-end run (with fakes) ─────────────────────────────────────────────


class _FakeMeshFull(_FakeMesh):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.created: list[dict] = []
        self.completions: list[dict] = []

    async def create_task(self, *, title, description, from_session_id):
        rec = {"title": title, "description": description, "from_session_id": from_session_id}
        self.created.append(rec)
        return {"id": f"next-doctor-{len(self.created)}"}

    async def complete(self, task_id, *, session_id, status, result):
        self.completions.append({
            "task_id": task_id, "session_id": session_id,
            "status": status, "result": result,
        })


class _FakeSettings:
    soul_session_id = "alfred-coo"


@pytest.mark.asyncio
async def test_doctor_run_queues_next_tick_and_completes_current(monkeypatch):
    """End-to-end: a doctor tick scans, posts (mocked), queues next kickoff,
    and marks current mesh task completed."""
    mesh = _FakeMeshFull()
    task = {
        "id": "doc-1",
        "title": "[persona:alfred-doctor] surveillance tick",
        "description": json.dumps({
            "interval_seconds": 60,
            "slack_channel": "C0BATCAVE",
            # Force a recent floor so mesh scan doesn't grab arbitrarily old data.
            "last_scan_ts": time.time() - 60.0,
        }),
    }
    persona = get_persona("alfred-doctor")
    settings = _FakeSettings()

    # Skip cadence sleep + skip slack post + skip linear network.
    async def no_sleep(_secs):
        return None

    async def no_post(*args, **kwargs):
        return None

    async def no_linear(**kwargs):
        return None

    # Skip subprocess journalctl call to keep the test fast and host-agnostic.
    def no_journal(**kwargs):
        kwargs["report"].add("journal_unavailable", detail="test-stub")

    monkeypatch.setattr("alfred_coo.autonomous_build.doctor.asyncio.sleep", no_sleep)
    monkeypatch.setattr("alfred_coo.autonomous_build.doctor.post_to_slack", no_post)
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.doctor.scan_linear_grounding_gaps", no_linear,
    )
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.doctor.scan_journal_via_subprocess", no_journal,
    )

    orch = AlfredDoctorOrchestrator(
        task=task, persona=persona, mesh=mesh, soul=None,
        dispatcher=None, settings=settings,
    )
    await orch.run()

    # Next-tick kickoff queued with same channel + interval + a fresh
    # last_scan_ts AND parent reference.
    assert len(mesh.created) == 1
    payload = json.loads(mesh.created[0]["description"])
    assert payload["interval_seconds"] == 60
    assert payload["slack_channel"] == "C0BATCAVE"
    assert payload["parent_doctor_task_id"] == "doc-1"
    assert payload["last_scan_ts"] > task["description"].count("0")  # any positive
    assert "[persona:alfred-doctor]" in mesh.created[0]["title"]

    # Current task marked completed (not failed) with summary + counters.
    assert len(mesh.completions) == 1
    comp = mesh.completions[0]
    assert comp["task_id"] == "doc-1"
    assert comp["status"] == "completed"
    assert "summary" in comp["result"]
    assert "counters" in comp["result"]


@pytest.mark.asyncio
async def test_run_playbooks_skipped_when_flag_missing(monkeypatch):
    """Playbooks are gated behind ``payload.playbooks_enabled=true``.
    When the flag is absent or false, ``_run_playbooks`` returns ``[]``
    without invoking any playbook ``execute``."""
    mesh = _FakeMeshFull()
    task = {
        "id": "doc-no-pb",
        "title": "[persona:alfred-doctor] surveillance tick",
        "description": json.dumps({
            "interval_seconds": 60,
            "last_scan_ts": time.time() - 60.0,
        }),
    }
    persona = get_persona("alfred-doctor")
    settings = _FakeSettings()

    invoked: list[str] = []

    class _SpyPlaybook:
        kind = "spy"
        max_actions_per_tick = 5

        async def execute(self, *, linear_api_key, dry_run):
            invoked.append("ran")
            return PlaybookResult(kind=self.kind, dry_run=dry_run)

    monkeypatch.setattr(
        "alfred_coo.autonomous_build.doctor.DEFAULT_PLAYBOOKS",
        [_SpyPlaybook()],
    )

    orch = AlfredDoctorOrchestrator(
        task=task, persona=persona, mesh=mesh, soul=None,
        dispatcher=None, settings=settings,
    )
    orch.payload = json.loads(task["description"])
    results = await orch._run_playbooks(linear_key="key")
    assert results == []
    assert invoked == []


@pytest.mark.asyncio
async def test_run_playbooks_invoked_when_flag_set(monkeypatch):
    """When ``playbooks_enabled=true`` is in the payload, registered
    playbooks fire and their results are returned. ``playbook_dry_run``
    defaults True (safety-first) so untouched payloads stay dry."""
    mesh = _FakeMeshFull()
    persona = get_persona("alfred-doctor")
    settings = _FakeSettings()

    received_dry: list[bool] = []

    class _SpyPlaybook:
        kind = "spy"
        max_actions_per_tick = 5

        async def execute(self, *, linear_api_key, dry_run):
            received_dry.append(dry_run)
            return PlaybookResult(
                kind=self.kind,
                candidates_found=2,
                dry_run=dry_run,
            )

    monkeypatch.setattr(
        "alfred_coo.autonomous_build.doctor.DEFAULT_PLAYBOOKS",
        [_SpyPlaybook()],
    )

    orch = AlfredDoctorOrchestrator(
        task={"id": "doc-pb-on", "title": "x", "description": "{}"},
        persona=persona, mesh=mesh, soul=None,
        dispatcher=None, settings=settings,
    )
    orch.payload = {"playbooks_enabled": True}
    results = await orch._run_playbooks(linear_key="k")
    assert len(results) == 1
    assert results[0].candidates_found == 2
    assert received_dry == [True]


@pytest.mark.asyncio
async def test_run_playbooks_swallows_playbook_crash(monkeypatch):
    """A playbook's ``execute`` raising must NOT break the chain.
    The doctor returns a PlaybookResult with the error recorded so the
    digest still surfaces the crash."""
    mesh = _FakeMeshFull()
    persona = get_persona("alfred-doctor")
    settings = _FakeSettings()

    class _CrashingPlaybook:
        kind = "crasher"
        max_actions_per_tick = 5

        async def execute(self, *, linear_api_key, dry_run):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "alfred_coo.autonomous_build.doctor.DEFAULT_PLAYBOOKS",
        [_CrashingPlaybook()],
    )

    orch = AlfredDoctorOrchestrator(
        task={"id": "doc-crash-pb", "title": "x", "description": "{}"},
        persona=persona, mesh=mesh, soul=None,
        dispatcher=None, settings=settings,
    )
    orch.payload = {"playbooks_enabled": True}
    results = await orch._run_playbooks(linear_key="k")
    assert len(results) == 1
    assert results[0].kind == "crasher"
    assert any("RuntimeError" in e for e in results[0].errors)


@pytest.mark.asyncio
async def test_doctor_run_marks_failed_on_unexpected_crash(monkeypatch):
    """If _run_inner raises, the top-level run() must mark the kickoff
    failed (so the chain doesn't double-handle) — the next link in the
    chain comes from a previously-queued kickoff anyway."""
    mesh = _FakeMeshFull()
    task = {
        "id": "doc-crash",
        "title": "[persona:alfred-doctor] surveillance tick",
        "description": "{",  # malformed JSON to set up baseline; payload parser tolerates this
    }
    persona = get_persona("alfred-doctor")
    settings = _FakeSettings()

    async def boom(self):
        raise RuntimeError("simulated scan crash")

    monkeypatch.setattr(
        AlfredDoctorOrchestrator, "_run_inner", boom,
    )

    orch = AlfredDoctorOrchestrator(
        task=task, persona=persona, mesh=mesh, soul=None,
        dispatcher=None, settings=settings,
    )
    await orch.run()

    assert len(mesh.completions) == 1
    comp = mesh.completions[0]
    assert comp["status"] == "failed"
    assert "simulated scan crash" in comp["result"]["error"]
