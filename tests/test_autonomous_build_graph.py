"""AB-04 tests: ticket graph construction.

Exercises `alfred_coo.autonomous_build.graph.build_ticket_graph` without
touching the real Linear GraphQL API. The builder takes injectable
fetcher callables so we don't have to monkeypatch module globals.
"""

from __future__ import annotations

import pytest

from alfred_coo.autonomous_build.graph import (
    TicketStatus,
    _parse_code,
    build_ticket_graph,
)


def _mk_issue(
    uuid: str,
    identifier: str,
    title: str,
    labels: list[str],
    state_name: str = "Backlog",
    estimate: int = 0,
    relations: list[dict] | None = None,
):
    return {
        "id": uuid,
        "identifier": identifier,
        "title": title,
        "labels": labels,
        "estimate": estimate,
        "state": {"name": state_name},
        "relations": relations or [],
    }


class _FakeListIssues:
    def __init__(self, issues):
        self.issues = issues
        self.calls = 0

    async def __call__(self, project_id, limit=250):
        self.calls += 1
        return {"issues": self.issues, "total": len(self.issues), "truncated": False}


async def _fake_relations_noop(issue_id):
    return {"error": "not called"}


async def test_graph_builds_from_linear_issues():
    """Builder pulls issues, assigns wave + epic from labels, parses codes."""
    issues = [
        _mk_issue(
            "uuid-1",
            "SAL-2583",
            "TIR-01 — tiresias-sovereign repo scaffold",
            ["wave-0", "tiresias", "size-M", "critical-path"],
            estimate=6,
        ),
        _mk_issue(
            "uuid-2",
            "SAL-2584",
            "TIR-02 — sovereign healthcheck endpoint",
            ["wave-1", "tiresias", "size-S"],
            estimate=3,
            relations=[
                {"type": "blocked_by",
                 "relatedIssue": {"id": "uuid-1", "identifier": "SAL-2583"}},
            ],
        ),
        _mk_issue(
            "uuid-3",
            "SAL-2585",
            "ALT-01 — aletheia daemon bootstrap",
            ["wave-0", "aletheia", "size-L"],
            estimate=10,
        ),
    ]
    fetcher = _FakeListIssues(issues)

    graph = await build_ticket_graph(
        project_id="proj-x",
        list_project_issues=fetcher,
        get_issue_relations=_fake_relations_noop,
    )

    assert fetcher.calls == 1
    assert len(graph) == 3

    t1 = graph.get_by_identifier("SAL-2583")
    assert t1 is not None
    assert t1.code == "TIR-01"
    assert t1.wave == 0
    assert t1.epic == "tiresias"
    assert t1.size == "M"
    assert t1.is_critical_path is True
    assert t1.estimate == 6

    t2 = graph.get_by_identifier("SAL-2584")
    assert t2 is not None
    assert t2.wave == 1
    # Edge wiring: t2 is blocked_by t1, so t1 blocks_out t2 and t2 blocks_in t1.
    assert "uuid-1" in t2.blocks_in
    assert "uuid-2" in t1.blocks_out

    t3 = graph.get_by_identifier("SAL-2585")
    assert t3.epic == "aletheia"
    assert t3.size == "L"
    assert not t3.is_critical_path


async def test_graph_ignores_relations_outside_project_batch():
    """A blocked_by pointing at an issue not in the batch is silently dropped."""
    issues = [
        _mk_issue(
            "uuid-1",
            "SAL-1",
            "OPS-01 something",
            ["wave-0", "ops"],
            relations=[
                {"type": "blocked_by",
                 "relatedIssue": {"id": "uuid-999", "identifier": "SAL-999"}},
            ],
        ),
    ]
    fetcher = _FakeListIssues(issues)
    graph = await build_ticket_graph(
        project_id="p",
        list_project_issues=fetcher,
    )
    t = graph.get_by_identifier("SAL-1")
    assert t.blocks_in == []


async def test_graph_maps_linear_state_to_status():
    """Restored Linear state names map back to TicketStatus so the
    orchestrator doesn't re-dispatch Done tickets."""
    issues = [
        _mk_issue("u-done", "SAL-1", "TIR-01 done", ["wave-0", "tiresias"],
                  state_name="Done"),
        _mk_issue("u-bl", "SAL-2", "TIR-02 backlog", ["wave-0", "tiresias"],
                  state_name="Backlog"),
        _mk_issue("u-inprog", "SAL-3", "TIR-03 inprog", ["wave-0", "tiresias"],
                  state_name="In Progress"),
    ]
    graph = await build_ticket_graph(
        project_id="p",
        list_project_issues=_FakeListIssues(issues),
    )
    assert graph.get_by_identifier("SAL-1").status == TicketStatus.MERGED_GREEN
    assert graph.get_by_identifier("SAL-2").status == TicketStatus.PENDING
    assert graph.get_by_identifier("SAL-3").status == TicketStatus.IN_PROGRESS


async def test_graph_error_surfaces_runtime_error():
    async def errfetch(project_id, limit=250):
        return {"error": "linear down"}

    with pytest.raises(RuntimeError, match="linear_list_project_issues error"):
        await build_ticket_graph(project_id="p", list_project_issues=errfetch)


# ── AB-14 (SAL-2699): _CODE_RE widen for F/D/E/H plan-doc prefixes ─────────
#
# Regression: live-run on 2026-04-23 showed `F08: soul-lite service...`
# (SAL-2616) parsed to an empty ticket.code because _CODE_RE only
# recognised TIR/ALT/C/FLEET/OPS/SS/AB/MC/SG. Children lost their
# plan-doc grep anchor and fabricated scope (off-scope PR rolled back).
# Widening to include F/D/E/H closes that defect; regex group 0 now also
# preserves the original separator so plan-doc searches match verbatim.


@pytest.mark.parametrize(
    "title, expected",
    [
        # AB-14 additions — single-letter F/D/E/H plan-doc prefixes.
        ("F08: soul-lite service...", "F08"),
        ("D03: ops layer seed...", "D03"),
        ("E02: soul-svc gap close...", "E02"),
        ("H01: child grounding...", "H01"),
        # Existing prefixes must still round-trip.
        ("OPS-01: mc-ops network...", "OPS-01"),
        ("C-26: multi-tenant...", "C-26"),
        ("TIR-05: sovereign healthcheck...", "TIR-05"),
        # SAL-OPS-01 should match the inner OPS-01 (SAL is not in the
        # prefix set; the \b-anchored second match wins).
        ("SAL-OPS-01: ...", "OPS-01"),
        # No code present → empty string (orchestrator will emit the
        # "(unparseable — escalate...)" fallback line).
        ("random no code here", ""),
    ],
)
def test_parse_code_widened_prefixes(title: str, expected: str) -> None:
    """AB-14: _CODE_RE must match F/D/E/H plus all existing prefixes, and
    _parse_code must preserve the original separator so plan-doc greps
    match verbatim (``F08`` stays ``F08``, not ``F-08``)."""
    assert _parse_code(title) == expected


def test_parse_code_underscore_normalised_to_dash() -> None:
    """Underscored separators are normalised to dashes. Plan docs use the
    dash form exclusively for multi-char prefixes, so this keeps
    downstream greps consistent without losing a match on legacy titles."""
    assert _parse_code("C_26: legacy underscore") == "C-26"


def test_parse_code_empty_title_returns_empty() -> None:
    """Empty-title guard: no crash, empty string, orchestrator fallback
    emits the escalate line."""
    assert _parse_code("") == ""
