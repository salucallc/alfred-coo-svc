"""SAL-3072 (2026-04-28) — wave-gate ratio with excused-in-denominator.

Mining sub findings (2026-04-28): 97% of wave-gate passes (83/86 in 7d
trailing window) were force-passes where the green-ratio denominator
excluded excused tickets, so a wave with 5 green and 9 excused looked
like ratio=1.00 (passed) when in reality only 5 of 14 actually shipped.
One pass even fired with denominator=0 ("no work, gate passed") because
the all-excused branch was a vacuous-truth pass.

This file pins the new contract: ``denominator = green + failed +
excused``. The default threshold (0.9 / SOFT_GREEN_THRESHOLD) is
unchanged; the bug was the formula, not the bar.

Test matrix mirrors the cases in the SAL-3072 spec, scaled to the
codebase's actual default (0.9 — the prompt's example used 0.50 which
is a separate tuning question):

    green | failed | excused | denom | ratio | result
    ------|--------|---------|-------|-------|-------
    5     |   0    |    9    |  14   | 0.36  | fail (was force-pass)
    9     |   1    |    0    |  10   | 0.90  | pass (soft-green)
    0     |   0    |    14   |  14   | 0.00  | fail (was force-pass)
    0     |   0    |    0    |   0   | 0.00  | fail (empty wave; was vacuous)
    9     |   1    |    0    |  10   | 0.90  | pass (full match w/ failure)
"""

from __future__ import annotations

import json

import pytest

from alfred_coo.autonomous_build.graph import (
    Ticket,
    TicketGraph,
    TicketStatus,
)
from alfred_coo.autonomous_build.orchestrator import (
    AutonomousBuildOrchestrator,
    HintStatus,
    VerificationResult,
)


# ── Minimal fakes (mirror tests/test_autonomous_build_orchestrator.py) ─────


class _FakeMesh:
    async def create_task(self, *, title, description="", from_session_id=None):
        return {"id": "child-x", "title": title, "status": "pending"}

    async def list_tasks(self, status=None, limit=50):
        return []

    async def complete(self, *a, **kw):
        return None


class _FakeSoul:
    def __init__(self):
        self.writes: list[dict] = []

    async def write_memory(self, content, topics=None):
        self.writes.append({"content": content, "topics": topics or []})
        return {"memory_id": f"m-{len(self.writes)}"}

    async def recent_memories(self, limit=5, topics=None):
        return []


class _FakeSettings:
    soul_session_id = "sal-3072-test"
    soul_node_id = "test-node"
    soul_harness = "pytest"


def _mk_persona():
    class P:
        name = "autonomous-build-a"
        handler = "AutonomousBuildOrchestrator"

    return P()


def _mk_orch(payload: dict | None = None) -> AutonomousBuildOrchestrator:
    desc = json.dumps(payload or {"linear_project_id": "PROJ-3072"})
    task = {
        "id": "kick-3072",
        "title": "[persona:autonomous-build-a] kickoff",
        "description": desc,
    }
    return AutonomousBuildOrchestrator(
        task=task,
        persona=_mk_persona(),
        mesh=_FakeMesh(),
        soul=_FakeSoul(),
        dispatcher=object(),
        settings=_FakeSettings(),
    )


def _seed(orch: AutonomousBuildOrchestrator, tickets: list[Ticket]) -> None:
    g = TicketGraph()
    for t in tickets:
        g.nodes[t.id] = t
        g.identifier_index[t.identifier] = t.id
    orch.graph = g


def _t(uuid: str, ident: str, code: str, wave: int = 1) -> Ticket:
    return Ticket(
        id=uuid,
        identifier=ident,
        code=code,
        title=f"{ident} {code}",
        wave=wave,
        epic="tiresias",
        size="M",
        estimate=5,
        is_critical_path=False,
    )


async def _patch_nosleep(monkeypatch):
    async def _ns(delay):
        return None

    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.asyncio.sleep", _ns
    )


def _excuse_path_conflict(orch: AutonomousBuildOrchestrator, t: Ticket) -> None:
    """Seed _verified_hints so _is_wave_gate_excused excuses ``t`` via
    the PATH_CONFLICT axis (mirrors the production excusal path most
    commonly observed in the mining-sub force-pass log)."""
    orch._verified_hints[t.code.upper()] = VerificationResult(
        code=t.code,
        hint=None,
        status=HintStatus.PATH_CONFLICT,
        repo_exists=True,
        path_results=(),
        error="path conflict",
    )


# ── The five SAL-3072 cases ────────────────────────────────────────────────


async def test_5_green_9_excused_fails_was_force_pass(monkeypatch):
    """Case 1 (the bug): 5 green / 0 failed / 9 excused.
    Pre-fix denominator=5 → ratio=1.00 → "passed" (force-pass).
    Post-fix denominator=14 → ratio≈0.36 → fails 0.9 threshold.
    """
    orch = _mk_orch()
    orch.poll_sleep_sec = 0
    greens = [_t(f"ug{i}", f"SAL-G{i}", f"TIR-G{i:02d}") for i in range(5)]
    excused = [_t(f"ue{i}", f"SAL-E{i}", f"TIR-E{i:02d}") for i in range(9)]
    for t in greens:
        t.status = TicketStatus.MERGED_GREEN
    for t in excused:
        t.status = TicketStatus.FAILED
        _excuse_path_conflict(orch, t)
    _seed(orch, greens + excused)
    await _patch_nosleep(monkeypatch)

    with pytest.raises(RuntimeError, match="nothing shipped|green_ratio"):
        await orch._wait_for_wave_gate(1)
    halt = next(
        e for e in orch.state.events
        if e["kind"] == "wave_halt_below_soft_green"
    )
    assert halt.get("excused_count") == 9
    # 5 / (5 + 0 + 9) = 5/14 ≈ 0.357
    assert halt.get("green_ratio") == pytest.approx(5 / 14, abs=1e-3)


async def test_5_green_0_failed_0_excused_passes_lower_threshold(monkeypatch):
    """Case 2: 5 green / 0 failed / 0 excused.
    denominator = 5, ratio = 1.0 → passes any threshold (including
    default 0.9). Override threshold to 0.5 to mirror the prompt's
    example numerically.
    """
    orch = _mk_orch(
        payload={
            "linear_project_id": "PROJ-3072",
            "wave_green_ratio_threshold": 0.5,
        }
    )
    orch._parse_payload()
    orch.poll_sleep_sec = 0
    greens = [_t(f"ug{i}", f"SAL-G{i}", f"TIR-G{i:02d}") for i in range(5)]
    for t in greens:
        t.status = TicketStatus.MERGED_GREEN
    _seed(orch, greens)
    await _patch_nosleep(monkeypatch)

    # No raise.
    await orch._wait_for_wave_gate(1)
    kinds = [e["kind"] for e in orch.state.events]
    assert "wave_all_green" in kinds
    assert "wave_halt_below_soft_green" not in kinds


async def test_0_green_14_excused_fails_was_vacuous_pass(monkeypatch):
    """Case 3: 0 green / 0 failed / 14 excused.
    Pre-fix denominator=0 → vacuous "skipped_all_excused" pass.
    Post-fix denominator=14, ratio=0.0 → fails. The mining-sub flagged
    this as the wave-0 force-pass scenario (36 times in a row, same 9
    tickets, never shipped, gate kept "passing").
    """
    orch = _mk_orch()
    orch.poll_sleep_sec = 0
    excused = [_t(f"ue{i}", f"SAL-E{i}", f"TIR-E{i:02d}") for i in range(14)]
    for t in excused:
        t.status = TicketStatus.FAILED
        _excuse_path_conflict(orch, t)
    _seed(orch, excused)
    await _patch_nosleep(monkeypatch)

    with pytest.raises(RuntimeError, match="nothing shipped|green_ratio"):
        await orch._wait_for_wave_gate(1)
    halt = next(
        e for e in orch.state.events
        if e["kind"] == "wave_halt_below_soft_green"
    )
    assert halt.get("excused_count") == 14
    assert halt.get("green_ratio") == pytest.approx(0.0)
    # Crucially: the old vacuous-pass event is NOT emitted.
    kinds = [e["kind"] for e in orch.state.events]
    assert "wave_all_excused" not in kinds


async def test_empty_wave_short_circuits(monkeypatch):
    """Case 4 (edge): wave with literally zero tickets — _wait_for_wave_gate
    returns early at the top of the function (``if not wave_tickets:
    return``) without emitting any wave-end event. This is intentional:
    an empty wave is a no-op (e.g. wave 5 doesn't exist for a 3-wave
    project) and shouldn't raise.

    The post-SAL-3072 contract treats "all tickets REPO_MISSING-filtered"
    as a hard failure (raised in ``_wait_for_wave_gate``), but a wave
    with no tickets at all is a different category — the orchestrator
    never had work to dispatch so there is no failure mode to surface.
    """
    orch = _mk_orch()
    orch.poll_sleep_sec = 0
    _seed(orch, [])
    await _patch_nosleep(monkeypatch)

    # No raise, no event.
    await orch._wait_for_wave_gate(1)
    assert orch.state.events == []


async def test_9_green_1_failed_passes_soft_green(monkeypatch):
    """Case 5 (regression): 9 green / 1 failed / 0 excused → ratio=0.90,
    matches default threshold 0.9 exactly → soft-green pass. Mirrors the
    pre-fix happy-path flow so we're confident the patch only changes
    the excused-bug branch and not the existing all-good path.
    """
    orch = _mk_orch()
    orch.poll_sleep_sec = 0
    tickets = [_t(f"u{i}", f"SAL-{i}", f"TIR-{i:02d}") for i in range(10)]
    for t in tickets[:9]:
        t.status = TicketStatus.MERGED_GREEN
    tickets[9].status = TicketStatus.FAILED
    _seed(orch, tickets)
    await _patch_nosleep(monkeypatch)

    # No raise.
    await orch._wait_for_wave_gate(1)
    soft = next(
        (e for e in orch.state.events if e["kind"] == "wave_soft_green"),
        None,
    )
    assert soft is not None, (
        f"expected wave_soft_green event; got "
        f"{[e['kind'] for e in orch.state.events]}"
    )
    assert soft.get("green_ratio") == pytest.approx(0.9)


# ── SAL-3676 (2026-04-29) — ESCALATED vs ABANDONED discriminator ──────────
#
# Tonight's MSSP-Ext orchestrator crashed at 07:19:45Z with
# ``green=2 failed=0 excused=4 of 6 ratio=0.33`` — three of those four
# excused tickets were hard-timeout abandonments (SAL-3539/3540/3541), one
# was a legitimate human-assigned skip (SAL-3545). Pre-fix the wave-gate
# excusal axis 4 matched any ESCALATED ticket, so the abandonments were
# masked as grounding-gaps and the operator-facing message read "nothing
# shipped" rather than "3 hard-timeout failures pulled the ratio down".
#
# These three tests pin the post-fix contract:
#
#   1. Grounding-gap escalations (legitimate ESCALATED) — STILL excused.
#      A wave with all-grounding-gap-escalations preserves the pre-fix
#      excusal behaviour (ratio = 0/N, fails on the threshold but the
#      message reflects the excused-only shape).
#
#   2. Hard-timeout abandonments (ABANDONED) — counted in the FAILED
#      column. A wave with hard-timeout abandonments brings the ratio
#      below threshold AND the wave-gate raise message says "X failures",
#      not "nothing shipped".
#
#   3. Mixed: legitimate ESCALATED + abandonment ABANDONED — the
#      ABANDONED bucket counts against the ratio, ESCALATED stays
#      excused. End-to-end discriminator parity check.


def _excuse_human_assigned(t: Ticket) -> None:
    """Seed the human-assigned label so axis 1 of _is_wave_gate_excused
    fires (independent of the SAL-3676 axis-4 ESCALATED-only check)."""
    t.labels = ["human-assigned"]


async def test_grounding_gap_escalated_still_excused_post_sal_3676(monkeypatch):
    """Case A: a wave full of legitimate grounding-gap escalations —
    every ticket is ESCALATED via the persona's `linear_create_issue`
    emit-mode (SAL-2886 path, _envelope_is_grounding_gap → True). All
    of them must stay excused (axis 4 still matches ESCALATED) so the
    pre-fix grounding-gap behaviour is preserved.

    With excused-in-denominator (SAL-3072) the wave still fails at
    ratio=0.0, but the operator-facing message is "(green=0
    excused=N); nothing shipped" — NOT "X non-critical failure(s)".
    That's the discriminator: ESCALATED tickets show up in the
    excused-dominant message bucket, not in the failed-pulled-the-
    ratio-down bucket.
    """
    orch = _mk_orch()
    orch.poll_sleep_sec = 0
    tickets = [_t(f"ueg{i}", f"SAL-EG{i}", f"TIR-EG{i:02d}") for i in range(4)]
    for t in tickets:
        t.status = TicketStatus.ESCALATED
    _seed(orch, tickets)
    await _patch_nosleep(monkeypatch)

    with pytest.raises(RuntimeError, match="nothing shipped|green_ratio"):
        await orch._wait_for_wave_gate(1)

    halt = next(
        e for e in orch.state.events
        if e["kind"] == "wave_halt_below_soft_green"
    )
    # All 4 ESCALATED stay excused; failed bucket empty.
    assert halt.get("excused_count") == 4
    assert halt.get("failed") == [], (
        f"ESCALATED must NOT count in failed column post-SAL-3676; "
        f"got failed={halt.get('failed')}"
    )
    assert halt.get("green_ratio") == pytest.approx(0.0)


async def test_hard_timeout_abandoned_counts_as_failed_post_sal_3676(
    monkeypatch,
):
    """Case B (the bug we shipped to fix): a wave full of ABANDONED
    tickets (the new force-fail terminal that hard-timeout / phantom-
    loop / wave-stall set instead of ESCALATED) must count in the FAILED
    column. With 3 ABANDONED + 1 green out of 4, ratio = 1/4 = 0.25 —
    below default 0.9 threshold → wave halts. The raise message MUST
    reflect "3 non-critical failure(s)" not "nothing shipped" so an
    operator tailing logs sees the real failure count instead of a
    misleading grounding-gap masquerade.

    Pre-fix (SAL-3676 reproduction): all 3 ABANDONED would have been
    set to ESCALATED, axis-4 of _is_wave_gate_excused would have excused
    them, ratio = 1/1 = 1.00 → FORCE-PASS the wave despite 3 real
    failures. SAL-3072 (denominator math) flipped this to 1/4 = 0.25
    → fail, but the message still said "nothing shipped" — operator
    couldn't tell timeouts from grounding-gaps. Tonight's MSSP-Ext
    crash is the live evidence.
    """
    orch = _mk_orch()
    orch.poll_sleep_sec = 0
    green = _t("ug-1", "SAL-G-1", "TIR-G-01")
    green.status = TicketStatus.MERGED_GREEN
    abandoned = [
        _t(f"ua{i}", f"SAL-A{i}", f"TIR-A{i:02d}") for i in range(3)
    ]
    for t in abandoned:
        t.status = TicketStatus.ABANDONED
    _seed(orch, [green] + abandoned)
    await _patch_nosleep(monkeypatch)

    with pytest.raises(RuntimeError) as excinfo:
        await orch._wait_for_wave_gate(1)

    msg = str(excinfo.value)
    # Must surface the real failure count, NOT the excused-dominant shape.
    assert "non-critical failure" in msg, (
        f"raise message must surface the real failure count; got: {msg!r}"
    )
    assert "nothing shipped" not in msg, (
        f"ABANDONED must not be classified as excused/grounding-gap; "
        f"got: {msg!r}"
    )

    halt = next(
        e for e in orch.state.events
        if e["kind"] == "wave_halt_below_soft_green"
    )
    # ABANDONED ⇒ counted as failed.
    assert sorted(halt.get("failed") or []) == [
        "SAL-A0", "SAL-A1", "SAL-A2",
    ], (
        f"ABANDONED tickets must populate the failed bucket; "
        f"got failed={halt.get('failed')}"
    )
    assert halt.get("excused_count") == 0, (
        f"ABANDONED must NOT be excused; got "
        f"excused_count={halt.get('excused_count')}"
    )
    assert halt.get("green_ratio") == pytest.approx(1 / 4, abs=1e-3)


async def test_mixed_escalated_and_abandoned_split_correctly(monkeypatch):
    """Case C (end-to-end discriminator): a wave with one legitimate
    grounding-gap escalation + one human-assigned skip + three hard-
    timeout abandonments + two greens.

    Post-SAL-3676 contract:
      - The human-assigned ticket → excused via axis 1 (label).
      - The grounding-gap ticket  → excused via axis 4 (ESCALATED).
      - The three abandoned tickets → counted in the FAILED column.
      - The two greens → numerator.

    denominator = 2 green + 3 failed + 2 excused = 7
    ratio = 2 / 7 ≈ 0.286 → below 0.9 → halt with the failure-class
    message ("3 non-critical failure(s)").

    Mirrors tonight's MSSP-Ext crash shape (green=2 abandoned=3 excused=
    1 + 1) so we'd catch a regression of this exact incident. Pre-fix
    the three abandoned would have been ESCALATED + excused → ratio =
    2/2 = 1.00, force-pass the wave despite 3 real timeouts.
    """
    orch = _mk_orch()
    orch.poll_sleep_sec = 0

    greens = [_t(f"ug{i}", f"SAL-G{i}", f"TIR-G{i:02d}") for i in range(2)]
    for t in greens:
        t.status = TicketStatus.MERGED_GREEN

    # Legitimate ESCALATED via the grounding-gap path (no label, axis 4).
    grounding_gap = _t("ueg", "SAL-EG", "TIR-EG")
    grounding_gap.status = TicketStatus.ESCALATED

    # Legitimate excusal via the human-assigned label (axis 1).
    human = _t("uh", "SAL-H", "TIR-H")
    human.status = TicketStatus.ESCALATED  # shape from line 4149 of orch
    _excuse_human_assigned(human)

    abandoned = [_t(f"ua{i}", f"SAL-A{i}", f"TIR-A{i:02d}") for i in range(3)]
    for t in abandoned:
        t.status = TicketStatus.ABANDONED

    _seed(orch, greens + [grounding_gap, human] + abandoned)
    await _patch_nosleep(monkeypatch)

    with pytest.raises(RuntimeError) as excinfo:
        await orch._wait_for_wave_gate(1)

    msg = str(excinfo.value)
    assert "non-critical failure" in msg, (
        f"raise message must surface the real failure count; got: {msg!r}"
    )

    halt = next(
        e for e in orch.state.events
        if e["kind"] == "wave_halt_below_soft_green"
    )
    # ABANDONED → failed bucket; ESCALATED + human-assigned → excused.
    assert sorted(halt.get("failed") or []) == [
        "SAL-A0", "SAL-A1", "SAL-A2",
    ], (
        f"only ABANDONED must populate the failed bucket; got "
        f"failed={halt.get('failed')}"
    )
    assert halt.get("excused_count") == 2, (
        f"ESCALATED + human-assigned must total 2 excused; got "
        f"excused_count={halt.get('excused_count')}"
    )
    # 2 / (2 + 3 + 2) = 2/7 ≈ 0.286
    assert halt.get("green_ratio") == pytest.approx(2 / 7, abs=1e-3)
