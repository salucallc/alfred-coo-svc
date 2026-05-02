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
    _parse_wave,
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


@pytest.mark.parametrize(
    "title, expected",
    [
        # Hint-batch-2: alfred-doctor children use a letters-only suffix
        # (`AD-a` … `AD-h`, no digits). Without the second alternation
        # branch in _CODE_RE these fell into no_hint_no_code and stalled
        # on wave-3 (SAL-3281..3288).
        ("[AD-a] Ingest service + SQLite timeseries schema", "AD-A"),
        ("[AD-h] Dashboard integration (v8-doctor route)", "AD-H"),
        ("AD_d underscore variant", "AD-D"),
        # Out-of-range suffix must NOT match — only a..h are valid for the
        # alfred-doctor epic. Anything else falls through to the standard
        # alternation (which requires digits → empty).
        ("AD-i should not match", ""),
        ("AD-z also no match", ""),
    ],
)
def test_parse_code_alfred_doctor_letter_suffix(title: str, expected: str) -> None:
    """Hint-batch-2: _CODE_RE recognises AD-[a-h] (letters only, no
    digits) so the alfred-doctor children resolve to a real plan-doc
    code and pick up their _TARGET_HINTS entry."""
    assert _parse_code(title) == expected


def test_parse_code_empty_title_returns_empty() -> None:
    """Empty-title guard: no crash, empty string, orchestrator fallback
    emits the escalate line."""
    assert _parse_code("") == ""


@pytest.mark.parametrize(
    "title, expected",
    [
        # wave-1 silent-complete fix (2026-04-29): MSSP extraction track.
        # Before this fix, every title parsed to '' and the rendered
        # ## Target block was `(unresolved)`, triggering the persona's
        # Step 0 grounding-gap escalate path. Both kickoffs (0de3e2be +
        # dae5a5c0) crashed with green=0/excused=N as a result.
        (
            "MSSP-EX-A — Extract GitHub identity refs (org, allowlist, QA bot)",
            "MSSP-EX-A",
        ),
        ("MSSP-EX-B — Extract GHCR image refs in workflows + compose", "MSSP-EX-B"),
        ("MSSP-EX-C — Extract Linear team key + UUID + ticket regex", "MSSP-EX-C"),
        (
            "MSSP-EX-D — Extract Oracle infrastructure refs (HIGH RISK)",
            "MSSP-EX-D",
        ),
        ("MSSP-EX-E — Extract Slack channel + token + operator user-id", "MSSP-EX-E"),
        ("MSSP-EX-H — Add MSSPSettings + MSSP install identity vars", "MSSP-EX-H"),
        # wave-1 silent-complete fix: MSSP federation track. Titles use
        # "MSSP Federation W1-A: ..." (a SPACE between MSSP and
        # Federation). _parse_code normalises that phrase to MSSP-FED-W1-A
        # before regex search so the federation tickets resolve to a
        # non-empty code.
        (
            "MSSP Federation W1-A: schema for grants + audit + pubkeys",
            "MSSP-FED-W1-A",
        ),
        (
            "MSSP Federation W1-B: scope catalog YAML + loader + contract tests",
            "MSSP-FED-W1-B",
        ),
        (
            "MSSP Federation W1-C: SQL functions for issue / revoke / renew",
            "MSSP-FED-W1-C",
        ),
        # Multi-digit wave numbers should also normalise correctly.
        (
            "MSSP Federation W2-A: phase-2 enforcement",
            "MSSP-FED-W2-A",
        ),
        # MSSP-EX missing trailing letter → must not match (avoids
        # accidental partial-prefix matches on unrelated MSSP-EX prose).
        ("MSSP-EX standalone with no suffix", ""),
        # MSSP without -EX or Federation prefix should not match.
        ("MSSP general write-up", ""),
    ],
)
def test_parse_code_mssp_track_titles(title: str, expected: str) -> None:
    """wave-1 silent-complete fix (2026-04-29): _CODE_RE recognises
    MSSP-EX-{A..Z} and MSSP-FED-W{N}-{A..Z}, and _parse_code normalises
    the federation title's "MSSP Federation W1-A" phrase into the
    canonical MSSP-FED-W1-A token. Closes the NO_HINT escalation
    spiral on kickoffs 0de3e2be (MSSP-EX retry) + dae5a5c0 (MSSP
    Federation wave-1)."""
    assert _parse_code(title) == expected


def test_target_hints_cover_mssp_wave_1_codes() -> None:
    """wave-1 silent-complete fix (2026-04-29): every MSSP wave-1 code
    that _parse_code now extracts MUST have a `_TARGET_HINTS` entry,
    otherwise dispatch still ends in the NO_HINT (unresolved) escalate
    path. Test pinned so a future ticket-rename can't silently regress
    the wave-gate (kickoff would crash again the same way)."""
    from alfred_coo.autonomous_build.orchestrator import _TARGET_HINTS

    expected_codes = {
        "MSSP-EX-A",
        "MSSP-EX-B",
        "MSSP-EX-C",
        "MSSP-EX-D",
        "MSSP-EX-E",
        "MSSP-EX-H",
        "MSSP-FED-W1-A",
        "MSSP-FED-W1-B",
        "MSSP-FED-W1-C",
    }
    missing = expected_codes - set(_TARGET_HINTS.keys())
    assert not missing, (
        f"_TARGET_HINTS missing wave-1 MSSP codes {sorted(missing)}; "
        "without these the persona's Step 0 grounding-gap path fires "
        "on every dispatch and the wave-gate crashes with green=0."
    )


@pytest.mark.parametrize(
    "title, labels, expected",
    [
        # wave-1 silent-complete fix follow-up (2026-04-29 evening):
        # Cockpit Consumer UX track. Long-form bracket prefix is
        # normalised unconditionally (no label gate needed).
        (
            "[Cockpit Consumer UX W1-A] Cockpit theme schema, loader, "
            "alias-table, validation",
            ["track:cockpit-consumer-ux", "wave-1"],
            "CO-W1-A",
        ),
        (
            "[Cockpit Consumer UX W1-B] Ship 7 new theme packs "
            "(synthwave-1989, brutalist, ...)",
            ["track:cockpit-consumer-ux", "wave-1"],
            "CO-W1-B",
        ),
        (
            "[Cockpit Consumer UX W1-C] Document the 5 existing themes "
            "against the new theme schema",
            ["track:cockpit-consumer-ux", "wave-1"],
            "CO-W1-C",
        ),
        # Cockpit prefix is unambiguous; even WITHOUT labels it parses
        # (the regex anchors on the inline track name).
        (
            "[Cockpit Consumer UX W2-A] later wave",
            None,
            "CO-W2-A",
        ),
        # Underscore variant inside the bracket should still match
        # (defensive — humans drift between dash and underscore).
        (
            "[Cockpit Consumer UX W1_D] underscore wave-key variant",
            ["track:cockpit-consumer-ux"],
            "CO-W1-D",
        ),
        # Agent Ingest track uses the BARE "[W1-A]" prefix; only the
        # presence of `track:agent-ingest` label triggers the AI-
        # normalisation. Without the label, the bare bracket should
        # parse to '' (unmatched) — safety against accidental over-match
        # on unrelated tickets that might use a bare wave bracket.
        (
            "[W1-A] Plugin SDK base — direction-aware SalucaPlugin ABC",
            ["track:agent-ingest", "wave-1"],
            "AI-W1-A",
        ),
        (
            "[W1-B] Soul-svc soulkey issuance API for external agents",
            ["track:agent-ingest", "wave-1"],
            "AI-W1-B",
        ),
        (
            "[W1-C] Inbound reference plugin saluca-plugin-echo-inbound",
            ["track:agent-ingest", "wave-1"],
            "AI-W1-C",
        ),
        (
            "[W1-D] Outbound reference plugin saluca-plugin-echo-outbound",
            ["track:agent-ingest", "wave-1"],
            "AI-W1-D",
        ),
        # Bare "[W1-A]" WITHOUT `track:agent-ingest` label MUST NOT
        # over-match. Closes the over-match risk.
        (
            "[W1-A] some unrelated ticket without the agent-ingest track",
            ["track:something-else", "wave-1"],
            "",
        ),
        (
            "[W1-A] no labels at all",
            None,
            "",
        ),
    ],
)
def test_parse_code_co_ai_track_titles(
    title: str, labels: list[str] | None, expected: str
) -> None:
    """wave-1 silent-complete fix follow-up (2026-04-29 evening):
    _CODE_RE recognises CO-W{N}-{A..Z} and AI-W{N}-{A..Z}. _parse_code
    normalises "[Cockpit Consumer UX W1-A]" -> "CO-W1-A"
    unconditionally (long-form prefix is unique to that track) and
    "[W1-A]" -> "AI-W1-A" only when `track:agent-ingest` is in labels
    (label-gated to avoid over-matching unrelated bare brackets).
    Closes the NO_HINT escalation spiral that would have hit
    SAL-3591/3592/3593 (Cockpit-UX wave-1) and SAL-3609/3610/3611/3612
    (Agent-Ingest wave-1) had the kickoffs fired against the original
    PR #302 regex."""
    assert _parse_code(title, labels=labels) == expected


def test_target_hints_cover_co_ai_wave_1_codes() -> None:
    """wave-1 silent-complete fix follow-up (2026-04-29 evening): every
    Cockpit-UX + Agent-Ingest wave-1 code that _parse_code now extracts
    MUST have a `_TARGET_HINTS` entry, otherwise dispatch still ends in
    NO_HINT (unresolved) escalate path. Pinned so a future ticket-rename
    can't silently regress the wave-gate."""
    from alfred_coo.autonomous_build.orchestrator import _TARGET_HINTS

    expected_codes = {
        "CO-W1-A",
        "CO-W1-B",
        "CO-W1-C",
        "AI-W1-A",
        "AI-W1-B",
        "AI-W1-C",
        "AI-W1-D",
    }
    missing = expected_codes - set(_TARGET_HINTS.keys())
    assert not missing, (
        f"_TARGET_HINTS missing wave-1 CO/AI codes {sorted(missing)}; "
        "without these the persona's Step 0 grounding-gap path fires "
        "on every Cockpit-UX or Agent-Ingest wave-1 dispatch and the "
        "wave-gate crashes with green=0."
    )


# ── Substrate task #88: _parse_wave accepts both wave-N and wave:N ──────────
#
# Regression: 2026-05-02 ~05:00Z, MC Metrics Library kickoff `49c36d2a`
# completed in 10s because sub-C used colon-separated `wave:N` labels when
# creating the Linear tickets. _parse_wave used `re.fullmatch(r"wave-(\d+)")`
# which only accepted hyphen → all 5 tickets parsed as wave=-1, dropped by
# graph filter. Postel's law fix: accept both separators.


@pytest.mark.parametrize(
    "label, expected_wave",
    [
        ("wave-0", 0),
        ("wave-1", 1),
        ("wave-12", 12),
        ("wave:0", 0),
        ("wave:1", 1),
        ("wave:12", 12),
        ("WAVE-3", 3),
        ("Wave:5", 5),
        ("  wave-2  ", 2),
        ("not-a-wave", -1),
        ("wave1", -1),
        ("wave--1", -1),
        ("", -1),
    ],
)
def test_parse_wave_accepts_hyphen_or_colon(label, expected_wave):
    """Both `wave-N` (canonical) and `wave:N` (legacy/sub-emitted) parse correctly."""
    assert _parse_wave([label]) == expected_wave


def test_parse_wave_first_wave_label_wins():
    """Mixed-format labels: first matching label wins (canonical or legacy)."""
    assert _parse_wave(["track:agent-ingest", "wave:2", "size-S"]) == 2
    assert _parse_wave(["track:agent-ingest", "wave-2", "size-S"]) == 2
    assert _parse_wave(["wave:2", "wave-3"]) == 2  # first one wins
