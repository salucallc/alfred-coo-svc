"""Tests for the refresh_dashboard_next_gate playbook.

Covers:
* Pure helpers: ``_render_paragraph``, ``_replace_next_gate_section``,
  ``_read_daemon_head`` (graceful when git missing).
* Idempotency — same inputs produce same paragraph.
* Missing doc → error result, no raise.
* Header missing → playbook appends a fresh ``## Next Gate`` section.
* Dry-run → no file write.
* Wet-run → file body is rewritten.
* Permission error → recorded as ``PlaybookResult.errors``, doesn't crash.
* Default registry includes the new playbook.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from alfred_coo.autonomous_build.playbooks import (
    DEFAULT_PLAYBOOKS,
    PlaybookResult,
    RefreshDashboardNextGatePlaybook,
)
from alfred_coo.autonomous_build.playbooks.refresh_dashboard_next_gate import (
    NEXT_GATE_HEADER,
    _read_daemon_head,
    _render_paragraph,
    _replace_next_gate_section,
)


# ── Pure helpers ────────────────────────────────────────────────────────────


_TEST_PLAYBOOK_KINDS = ("hydrate_apev_headings", "refresh_dashboard_next_gate")


def test_render_paragraph_byte_stable_for_same_inputs():
    """Same head + tick count + timestamp → identical output. Required so
    re-runs on unchanged state don't churn the file body unnecessarily."""
    a = _render_paragraph(
        head="abc1234", recent_doctor_ticks=12,
        now_iso="2026-05-01T20:00:00Z", interval_min=5,
        playbook_kinds=_TEST_PLAYBOOK_KINDS,
    )
    b = _render_paragraph(
        head="abc1234", recent_doctor_ticks=12,
        now_iso="2026-05-01T20:00:00Z", interval_min=5,
        playbook_kinds=_TEST_PLAYBOOK_KINDS,
    )
    assert a == b


def test_render_paragraph_includes_signals():
    out = _render_paragraph(
        head="abc1234", recent_doctor_ticks=7,
        now_iso="2026-05-01T20:00:00Z", interval_min=10,
        playbook_kinds=_TEST_PLAYBOOK_KINDS,
    )
    assert "abc1234" in out
    assert "7 doctor ticks" in out
    assert "2026-05-01T20:00:00Z" in out
    assert "every 10 min" in out


def test_render_paragraph_singular_tick_grammar():
    """One tick should read 'tick' not 'ticks' — small but visible polish
    on the dashboard."""
    out = _render_paragraph(
        head="x", recent_doctor_ticks=1,
        now_iso="2026-05-01T20:00:00Z", interval_min=5,
        playbook_kinds=_TEST_PLAYBOOK_KINDS,
    )
    assert "1 doctor tick in the last hour" in out
    assert "1 doctor ticks" not in out


def test_render_paragraph_handles_unknown_head():
    """Empty head string → human-readable 'unknown' clause; no crash."""
    out = _render_paragraph(
        head="", recent_doctor_ticks=0,
        now_iso="2026-05-01T20:00:00Z", interval_min=5,
        playbook_kinds=_TEST_PLAYBOOK_KINDS,
    )
    assert "daemon HEAD unknown" in out


def test_render_paragraph_lists_all_registered_playbooks():
    """The paragraph reflects the live ``DEFAULT_PLAYBOOKS`` list — adding
    a new playbook (e.g. ``restart_stalled_chains``) automatically shows
    up on the dashboard the next refresh tick. No hardcoded list."""
    out = _render_paragraph(
        head="x", recent_doctor_ticks=10,
        now_iso="2026-05-01T20:00:00Z", interval_min=5,
        playbook_kinds=(
            "hydrate_apev_headings",
            "refresh_dashboard_next_gate",
            "restart_stalled_chains",
        ),
    )
    assert "hydrate_apev_headings" in out
    assert "refresh_dashboard_next_gate" in out
    assert "restart_stalled_chains" in out


def test_render_paragraph_lists_pre_dispatch_gates():
    """The paragraph names every pre-dispatch gate that fires at startup
    (currently APE/V hydration + reference content hydration) so the
    operator dashboard reflects the full structural-fix surface area."""
    out = _render_paragraph(
        head="x", recent_doctor_ticks=10,
        now_iso="2026-05-01T20:00:00Z", interval_min=5,
        playbook_kinds=_TEST_PLAYBOOK_KINDS,
    )
    assert "APE/V hydration" in out
    assert "reference content hydration" in out


def test_render_paragraph_mentions_escalations_vs_errors_split():
    """Phase 3a metric stream tracks errors and escalations as distinct
    fields — the dashboard paragraph should reflect that so the operator
    knows the metric semantics they're seeing in Grafana are correct."""
    out = _render_paragraph(
        head="x", recent_doctor_ticks=10,
        now_iso="2026-05-01T20:00:00Z", interval_min=5,
        playbook_kinds=_TEST_PLAYBOOK_KINDS,
    )
    assert "errors" in out.lower()
    assert "escalation" in out.lower()


def test_render_paragraph_handles_empty_playbook_list():
    """Defensive: an empty ``playbook_kinds`` tuple renders cleanly (no
    crash) and reads as ``no playbooks registered`` so an unconfigured
    daemon still produces a parseable paragraph."""
    out = _render_paragraph(
        head="x", recent_doctor_ticks=0,
        now_iso="2026-05-01T20:00:00Z", interval_min=5,
        playbook_kinds=(),
    )
    assert "no playbooks registered" in out


# ── Section replacement ─────────────────────────────────────────────────────


def test_replace_next_gate_section_replaces_body_in_place():
    src = (
        "# Title\n\n"
        "## Next Gate\n\n"
        "Old stale paragraph that needs replacing.\n\n"
        "## Executive summary\n\n"
        "This part stays exactly as is.\n"
    )
    out = _replace_next_gate_section(src, "Fresh paragraph 2026-05-01.")
    assert "Old stale paragraph" not in out
    assert "Fresh paragraph 2026-05-01." in out
    # Non-target sections preserved verbatim.
    assert "## Executive summary" in out
    assert "This part stays exactly as is." in out


def test_replace_next_gate_section_appends_when_header_missing():
    """A markdown doc with no ``## Next Gate`` header gets one appended
    so the dashboard has something fresh to render on the next tick."""
    src = "# Title\n\nSome content with no Next Gate header.\n"
    out = _replace_next_gate_section(src, "Live paragraph.")
    assert NEXT_GATE_HEADER in out
    assert "Live paragraph." in out
    # Original content survives.
    assert "Some content with no Next Gate header." in out


def test_replace_next_gate_section_idempotent_when_input_unchanged():
    """Re-running the replacement with the same paragraph yields a body
    identical to the first run (so the playbook can decide to skip writing
    when ``new_src == src``)."""
    src = (
        "# T\n\n"
        "## Next Gate\n\n"
        "P1\n\n"
        "## Other\n"
    )
    once = _replace_next_gate_section(src, "REPLACEMENT")
    twice = _replace_next_gate_section(once, "REPLACEMENT")
    assert once == twice


def test_replace_next_gate_section_case_insensitive_header():
    src = "# T\n\n## next gate\n\nold\n\n## Other\n"
    out = _replace_next_gate_section(src, "new content")
    assert "new content" in out
    assert "old" not in out


# ── _read_daemon_head ──────────────────────────────────────────────────────


def test_read_daemon_head_returns_empty_when_repo_missing(tmp_path):
    """Pointing the helper at a non-repo dir must return '' (best-effort
    contract). The playbook handles empty-string head gracefully."""
    head = _read_daemon_head(str(tmp_path))
    assert head == ""


# ── Default registry ──────────────────────────────────────────────────────


def test_default_registry_contains_refresh_dashboard():
    kinds = [p.kind for p in DEFAULT_PLAYBOOKS]
    assert "refresh_dashboard_next_gate" in kinds


# ── execute() — file-backed behavior ──────────────────────────────────────


_SAMPLE_DOC = (
    "# Roadmap\n\n"
    "## Next Gate\n\n"
    "stale paragraph here\n\n"
    "## Executive summary\n\n"
    "untouched body.\n"
)


@pytest.mark.asyncio
async def test_execute_returns_error_when_doc_missing(tmp_path):
    """Doc not found → result reports error, returns gracefully."""
    pb = RefreshDashboardNextGatePlaybook(
        doc_path=str(tmp_path / "no-such-doc.md"),
    )
    res = await pb.execute(linear_api_key="k", dry_run=True)
    assert res.candidates_found == 0
    assert res.actions_taken == 0
    assert any("doc_not_found" in e for e in res.errors)


@pytest.mark.asyncio
async def test_execute_dry_run_does_not_write(tmp_path, monkeypatch):
    """Dry-run with a candidate change recorded but no file mutation."""
    doc = tmp_path / "roadmap.md"
    doc.write_text(_SAMPLE_DOC, encoding="utf-8")
    before_bytes = doc.read_bytes()
    pb = RefreshDashboardNextGatePlaybook(
        doc_path=str(doc), repo_path=str(tmp_path),
    )
    res = await pb.execute(linear_api_key="k", dry_run=True)
    assert res.candidates_found == 1
    assert res.actions_taken == 0
    assert res.dry_run is True
    assert any("would refresh" in n for n in res.notable)
    # File untouched in dry-run.
    assert doc.read_bytes() == before_bytes


@pytest.mark.asyncio
async def test_execute_wet_run_writes_replacement(tmp_path):
    doc = tmp_path / "roadmap.md"
    doc.write_text(_SAMPLE_DOC, encoding="utf-8")
    pb = RefreshDashboardNextGatePlaybook(
        doc_path=str(doc), repo_path=str(tmp_path),
    )
    res = await pb.execute(linear_api_key="k", dry_run=False)
    assert res.candidates_found == 1
    assert res.actions_taken == 1
    text = doc.read_text(encoding="utf-8")
    assert "stale paragraph here" not in text
    assert "Substrate self-healing live" in text
    # Adjacent untouched section survives.
    assert "## Executive summary" in text
    assert "untouched body." in text


@pytest.mark.asyncio
async def test_execute_idempotent_no_op_when_already_current(tmp_path):
    """Run twice in succession with the same inputs — second run finds
    nothing to do (the file already matches the rendered output)."""
    doc = tmp_path / "roadmap.md"
    doc.write_text(_SAMPLE_DOC, encoding="utf-8")
    pb = RefreshDashboardNextGatePlaybook(
        doc_path=str(doc), repo_path=str(tmp_path),
    )
    res1 = await pb.execute(linear_api_key="k", dry_run=False)
    assert res1.actions_taken == 1
    res2 = await pb.execute(linear_api_key="k", dry_run=False)
    # Second pass finds no candidate (file already current).
    assert res2.candidates_found == 0
    assert res2.actions_taken == 0


@pytest.mark.asyncio
async def test_execute_records_permission_error_without_raising(
    tmp_path, monkeypatch,
):
    """If the doc isn't writable by the daemon user, the playbook records
    a clear error and returns — does NOT raise into the doctor loop."""
    doc = tmp_path / "roadmap.md"
    doc.write_text(_SAMPLE_DOC, encoding="utf-8")
    pb = RefreshDashboardNextGatePlaybook(
        doc_path=str(doc), repo_path=str(tmp_path),
    )

    real_write_text = Path.write_text

    def deny(self, *args, **kwargs):
        if str(self) == str(doc):
            raise PermissionError(13, "denied")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", deny)
    res = await pb.execute(linear_api_key="k", dry_run=False)
    assert res.candidates_found == 1
    assert res.actions_taken == 0
    assert any("PermissionError" in e for e in res.errors)


@pytest.mark.asyncio
async def test_execute_uses_mesh_when_provided(tmp_path):
    """When ``mesh`` is passed via **_extra, the playbook's recent-tick
    counter populates from completed alfred-doctor tasks."""

    class _FakeMesh:
        async def list_tasks(self, *, status=None, limit=50):
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            if status == "completed":
                return [
                    {
                        "title": "[persona:alfred-doctor] surveillance tick",
                        "completed_at": now_iso,
                    },
                    {
                        "title": "[persona:alfred-doctor] surveillance tick",
                        "completed_at": now_iso,
                    },
                    # Non-doctor task — must be ignored.
                    {
                        "title": "[builder] SAL-9999",
                        "completed_at": now_iso,
                    },
                ]
            return []

    doc = tmp_path / "roadmap.md"
    doc.write_text(_SAMPLE_DOC, encoding="utf-8")
    pb = RefreshDashboardNextGatePlaybook(
        doc_path=str(doc), repo_path=str(tmp_path),
    )
    res = await pb.execute(
        linear_api_key="k", dry_run=False, mesh=_FakeMesh(),
    )
    assert res.actions_taken == 1
    text = doc.read_text(encoding="utf-8")
    assert "2 doctor ticks" in text
