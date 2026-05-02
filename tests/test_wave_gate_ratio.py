"""SAL-3919 (2026-05-02) — wave-gate ratio with excused-OUT-of-denominator.

History:
- SAL-3072 (2026-04-28) added excused tickets to the denominator to
  prevent a "force-pass bug" where a wave with 5 green and 9 excused
  reported ratio=1.00.
- Substrate fix #83 (PR #353, 2026-05-02) added Canceled / Cancelled /
  Duplicate Linear states as a fourth excusal axis. Combined with
  SAL-3072's denominator math, an Agent-Ingest kickoff (mesh task
  49e7fa40) hit green=1 / excused=3 / failed=0 / total=4 → ratio=0.25
  → halt at the 0.9 threshold even though 1/(4-3) = 1.0 should pass.
- SAL-3919 (this file) reverts the SAL-3072 denominator change.
  Excused tickets are work the orchestrator legitimately did NOT own
  this wave; they should not depress the green ratio for the work it
  DID own. When EVERY ticket is excused (denominator==0), the wave is
  a no-op and passes through with ratio=1.0.

The "force-pass" concern from SAL-3072 (ship-rate metrics) is a
metrics/observability question, not a gate question — the gate's job
is to halt when failures exceed the threshold, and an excused ticket
is not a failure.

Post-SAL-3919 contract: ``denominator = green + failed`` (i.e.
``len(scored)``); excused tickets are excluded. Default threshold
(0.9 / SOFT_GREEN_THRESHOLD) unchanged.

Test matrix:

    green | failed | excused | denom | ratio | result
    ------|--------|---------|-------|-------|-------
    5     |   0    |    9    |   5   | 1.00  | pass (was halt under SAL-3072)
    9     |   1    |    0    |  10   | 0.90  | pass (soft-green)
    0     |   0    |   14    |   0   | 1.00  | pass (was halt under SAL-3072)
    0     |   0    |    0    |   0   | 1.00  | pass (empty wave handled by short-circuit)
    9     |   1    |    0    |  10   | 0.90  | pass (full match w/ failure)
    1     |   2    |    1    |   3   | 0.33  | halt (real failures still gate)
    1     |   0    |    3    |   1   | 1.00  | halt-was-bug repro (SAL-3919)
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


async def test_5_green_9_excused_passes_post_sal_3919(monkeypatch):
    """Case 1 — SAL-3919 contract flip: 5 green / 0 failed / 9 excused.

    SAL-3072 history: pinned this case to fail with denominator=14 →
    ratio≈0.36 to mitigate a "force-pass" metrics concern (mining sub
    found 97% of passes had excused tickets in the trailing 7d window).

    SAL-3919 reverts that. Excused tickets are work the orchestrator
    legitimately did NOT own this wave; the gate should not punish a
    wave for excusals. Post-fix: denominator=5 (scored only), ratio=
    1.0, soft-green pass / all-green pass. The ship-rate metric is a
    separate observability concern.
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

    # No raise; wave passes.
    await orch._wait_for_wave_gate(1)
    kinds = [e["kind"] for e in orch.state.events]
    assert "wave_halt_below_soft_green" not in kinds
    assert "wave_all_green" in kinds, (
        f"expected wave_all_green; got {kinds}"
    )


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


async def test_0_green_14_excused_passes_post_sal_3919(monkeypatch):
    """Case 3 — SAL-3919: 0 green / 0 failed / 14 excused (all-excused).

    SAL-3072 pinned denominator=14 → ratio=0.0 → halt. SAL-3919 treats
    an all-excused wave as a no-op: denominator=0 short-circuits to
    ratio=1.0 and the gate advances. APE/V acceptance case 2 from the
    SAL-3919 spec.
    """
    orch = _mk_orch()
    orch.poll_sleep_sec = 0
    excused = [_t(f"ue{i}", f"SAL-E{i}", f"TIR-E{i:02d}") for i in range(14)]
    for t in excused:
        t.status = TicketStatus.FAILED
        _excuse_path_conflict(orch, t)
    _seed(orch, excused)
    await _patch_nosleep(monkeypatch)

    # No raise — gate advances silently.
    await orch._wait_for_wave_gate(1)
    kinds = [e["kind"] for e in orch.state.events]
    assert "wave_halt_below_soft_green" not in kinds, (
        f"all-excused wave must pass post-SAL-3919; got {kinds}"
    )
    # All-green branch fires (no failures, ratio>=threshold via fallback).
    assert "wave_all_green" in kinds


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

    SAL-3072 history: pinned this all-escalated wave to halt at
    ratio=0.0. SAL-3919 (this revision) treats all-excused waves as
    no-ops (denominator=0 → ratio=1.0 fallback → pass). The discriminator
    that this test exercises — ESCALATED is excused, not counted as
    failed — is unchanged; only the gate decision flipped.
    """
    orch = _mk_orch()
    orch.poll_sleep_sec = 0
    tickets = [_t(f"ueg{i}", f"SAL-EG{i}", f"TIR-EG{i:02d}") for i in range(4)]
    for t in tickets:
        t.status = TicketStatus.ESCALATED
    _seed(orch, tickets)
    await _patch_nosleep(monkeypatch)

    # No raise — all-excused wave passes through.
    await orch._wait_for_wave_gate(1)
    kinds = [e["kind"] for e in orch.state.events]
    assert "wave_halt_below_soft_green" not in kinds
    # Predicate-level: ESCALATED still excused (the SAL-3676 invariant).
    for t in tickets:
        assert orch._is_wave_gate_excused(t) is True, (
            f"ESCALATED ticket {t.identifier} must stay excused; "
            f"SAL-3676 invariant"
        )


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

    SAL-3919 contract: denominator = 2 green + 3 failed = 5 (excused
    excluded), ratio = 2 / 5 = 0.4 → below 0.9 → halt with the
    failure-class message ("3 non-critical failure(s)"). Pre-SAL-3919
    the denominator was 7 and the ratio 2/7 ≈ 0.286; both halt, but
    SAL-3919 surfaces the truer ship-rate among work the orchestrator
    actually owned.

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
    # SAL-3919: 2 / (2 + 3) = 2/5 = 0.4 (excused excluded from denom).
    assert halt.get("green_ratio") == pytest.approx(2 / 5, abs=1e-3)


# ── Substrate task #83 (2026-05-02) — Linear Canceled excusal axis ─────────
#
# SAL-3610 was correctly Canceled tonight after the soul-svc=identity /
# tiresias=policy architectural decision: the soulkey-issuance work the
# ticket scoped is already shipped in soul-svc, so the ticket was
# duplicative. But the wave-gate counted it as FAILED via
# graph._linear_state_to_status's "canceled" → TicketStatus.FAILED
# mapping — green=0/failed=1/excused=3/denom=4 → ratio=0.00, dragging
# the Agent-Ingest chain budget to zero across multiple wave-1 retries.
#
# Fix: _is_wave_gate_excused now treats ticket.linear_state ∈
# {Canceled, Cancelled, Duplicate} as an additional excusal axis (axis
# 4 in the comment numbering) so descoped tickets don't fail the gate.


async def test_canceled_linear_state_excused_post_task_83(monkeypatch):
    """A ticket with Linear state == Canceled must be excused from the
    wave-gate denominator regardless of its internal TicketStatus.

    SAL-3919 (2026-05-02): combined with the new "excused-out-of-
    denominator" math, an all-Canceled/ESCALATED wave is denominator=0
    → ratio=1.0 fallback → gate advances. Pre-SAL-3919 (substrate-#83
    only) the denominator was 4 and the gate halted at ratio=0.0 even
    though the Canceled ticket was correctly identified as excused.
    SAL-3919 closes the loop so descoped tickets don't halt the gate.
    """
    orch = _mk_orch()
    orch.poll_sleep_sec = 0

    canceled = _t("uc-3610", "SAL-3610", "AI-W1B")
    # Mirror what graph._linear_state_to_status produces for a Canceled
    # Linear state: status=FAILED, linear_state="Canceled".
    canceled.status = TicketStatus.FAILED
    canceled.linear_state = "Canceled"

    others = [_t(f"ueg{i}", f"SAL-EG{i}", f"AI-G{i:02d}") for i in range(3)]
    for t in others:
        t.status = TicketStatus.ESCALATED  # legitimate grounding-gap shape

    _seed(orch, [canceled] + others)
    await _patch_nosleep(monkeypatch)

    # Predicate: the Canceled ticket is excused (substrate-#83 invariant).
    assert orch._is_wave_gate_excused(canceled) is True

    # SAL-3919: all-excused wave passes through without halting.
    await orch._wait_for_wave_gate(1)
    kinds = [e["kind"] for e in orch.state.events]
    assert "wave_halt_below_soft_green" not in kinds, (
        f"all-excused wave (1 Canceled + 3 ESCALATED) must pass post-"
        f"SAL-3919; got {kinds}"
    )


async def test_duplicate_linear_state_also_excused(monkeypatch):
    """``Duplicate`` is the third terminal-not-our-fault state — same
    excusal contract as ``Canceled``. Mirrors the case-set already used
    by every "already terminal" bail-out elsewhere in the orchestrator.
    """
    orch = _mk_orch()
    orch.poll_sleep_sec = 0
    dup = _t("ud", "SAL-DUP", "AI-DUP")
    dup.status = TicketStatus.FAILED
    dup.linear_state = "Duplicate"
    assert orch._is_wave_gate_excused(dup) is True


async def test_lowercase_cancelled_spelling_excused(monkeypatch):
    """British / lowercase ``cancelled`` spelling is normalised the same
    way as ``Canceled`` (case-insensitive lower() match)."""
    orch = _mk_orch()
    orch.poll_sleep_sec = 0
    t = _t("ux", "SAL-X", "AI-X")
    t.status = TicketStatus.FAILED
    t.linear_state = "cancelled"  # lowercase, double-l British spelling
    assert orch._is_wave_gate_excused(t) is True


# ── SAL-3919 (2026-05-02) — wave-gate excused-out-of-denominator APE/V ─────
#
# Reproduces the Agent-Ingest kickoff failure (mesh task 49e7fa40-0001-
# 4e5e-9cfb-a9023f4defb2) and pins the four-case acceptance matrix from
# the SAL-3919 spec. With the SAL-3072 denominator math, a wave with
# green=1, excused=3, failed=0, total=4 reported ratio=1/4=0.25 and
# halted at the 0.9 threshold even though 1/(4-3)=1.0 should pass.
# Post-fix: excused tickets are excluded from the denominator; an all-
# excused wave (denominator=0) short-circuits to ratio=1.0.


async def test_sal_3919_case_1_one_green_three_excused_passes(monkeypatch):
    """APE/V case 1: green=1, excused=3, failed=0, total=4.
    denominator = total - excused = 1; ratio = 1/1 = 1.0; gate passes.
    Direct repro of mesh task 49e7fa40 — Agent-Ingest wave 1 with
    SAL-3612, SAL-3611, SAL-3609 all Cancelled/Duplicate.
    """
    orch = _mk_orch()
    orch.poll_sleep_sec = 0

    green = _t("ug-ai-1", "SAL-AI-G1", "AI-W1A")
    green.status = TicketStatus.MERGED_GREEN

    canceled_ids = ["SAL-3612", "SAL-3611", "SAL-3609"]
    canceled = []
    for ident in canceled_ids:
        t = _t(f"uc-{ident}", ident, f"AI-{ident}")
        t.status = TicketStatus.FAILED
        t.linear_state = "Cancelled"
        canceled.append(t)

    _seed(orch, [green] + canceled)
    await _patch_nosleep(monkeypatch)

    # No raise; gate advances.
    await orch._wait_for_wave_gate(1)
    kinds = [e["kind"] for e in orch.state.events]
    assert "wave_halt_below_soft_green" not in kinds, (
        f"SAL-3919 case 1: gate must advance with 1 green + 3 excused; "
        f"got {kinds}"
    )
    # Ratio computed against scored-only denominator: 1/1 = 1.0 → all-green.
    assert "wave_all_green" in kinds, (
        f"expected wave_all_green; got {kinds}"
    )


async def test_sal_3919_case_2_all_excused_passes_silently(monkeypatch):
    """APE/V case 2: green=0, excused=4, failed=0, total=4.
    denominator = 4-4 = 0; ratio falls back to 1.0; gate passes silently.
    """
    orch = _mk_orch()
    orch.poll_sleep_sec = 0

    excused = []
    for i in range(4):
        t = _t(f"ux{i}", f"SAL-X{i}", f"AI-X{i}")
        t.status = TicketStatus.FAILED
        t.linear_state = "Cancelled"
        excused.append(t)

    _seed(orch, excused)
    await _patch_nosleep(monkeypatch)

    # No raise — denominator=0 fallback.
    await orch._wait_for_wave_gate(1)
    kinds = [e["kind"] for e in orch.state.events]
    assert "wave_halt_below_soft_green" not in kinds, (
        f"SAL-3919 case 2: all-excused wave must pass silently; got {kinds}"
    )


async def test_sal_3919_case_3_real_failures_still_halt(monkeypatch):
    """APE/V case 3 (regression): green=1, excused=1, failed=2, total=4.
    denominator = 4-1 = 3; ratio = 1/3 ≈ 0.333; gate halts at 0.9.
    Confirms that real failures still gate the wave even with excused
    tickets removed from the denominator.
    """
    orch = _mk_orch()
    orch.poll_sleep_sec = 0

    green = _t("ug-r3", "SAL-R3-G", "AI-R3-G")
    green.status = TicketStatus.MERGED_GREEN

    canceled = _t("uc-r3", "SAL-R3-C", "AI-R3-C")
    canceled.status = TicketStatus.FAILED
    canceled.linear_state = "Cancelled"

    failed = [_t(f"uf-r3-{i}", f"SAL-R3-F{i}", f"AI-R3-F{i}") for i in range(2)]
    for t in failed:
        t.status = TicketStatus.FAILED  # non-excused failure

    _seed(orch, [green, canceled] + failed)
    await _patch_nosleep(monkeypatch)

    with pytest.raises(RuntimeError, match="non-critical failure|green_ratio"):
        await orch._wait_for_wave_gate(1)

    halt = next(
        e for e in orch.state.events
        if e["kind"] == "wave_halt_below_soft_green"
    )
    # excused = 1 (Cancelled), failed = 2 (the two SAL-R3-F*), green = 1.
    assert halt.get("excused_count") == 1
    assert sorted(halt.get("failed") or []) == ["SAL-R3-F0", "SAL-R3-F1"]
    # SAL-3919 ratio: 1/(1+2) = 1/3.
    assert halt.get("green_ratio") == pytest.approx(1 / 3, abs=1e-3)


async def test_sal_3919_case_4_fully_green_still_passes(monkeypatch):
    """APE/V case 4 (regression): green=4, excused=0, failed=0, total=4.
    denominator = 4-0 = 4; ratio = 4/4 = 1.0; gate passes.
    Confirms fully-green waves still pass identically post-fix.
    """
    orch = _mk_orch()
    orch.poll_sleep_sec = 0

    greens = [_t(f"ug-r4-{i}", f"SAL-R4-G{i}", f"AI-R4-G{i}") for i in range(4)]
    for t in greens:
        t.status = TicketStatus.MERGED_GREEN

    _seed(orch, greens)
    await _patch_nosleep(monkeypatch)

    # No raise; all-green pass.
    await orch._wait_for_wave_gate(1)
    kinds = [e["kind"] for e in orch.state.events]
    assert "wave_all_green" in kinds, f"expected wave_all_green; got {kinds}"
    assert "wave_halt_below_soft_green" not in kinds


async def test_sal_3919_halt_event_payload_consistent(monkeypatch):
    """The `green_ratio` field on the wave_halt_below_soft_green event
    must match the gate decision. With excused excluded from the
    denominator, the logged ratio mirrors the math the gate used.
    Regression check that the event payload didn't drift from the
    decision math (per SAL-3919 spec: "Apply the same logic in any
    `wave_halt_below_soft_green` event payload so the logged
    `green_ratio` matches the gate decision.").
    """
    orch = _mk_orch()
    orch.poll_sleep_sec = 0

    green = _t("ug-cons", "SAL-CONS-G", "AI-CONS-G")
    green.status = TicketStatus.MERGED_GREEN

    canceled = _t("uc-cons", "SAL-CONS-C", "AI-CONS-C")
    canceled.status = TicketStatus.FAILED
    canceled.linear_state = "Duplicate"

    failed_t = [_t(f"uf-cons-{i}", f"SAL-CONS-F{i}", f"AI-CONS-F{i}") for i in range(3)]
    for t in failed_t:
        t.status = TicketStatus.FAILED

    _seed(orch, [green, canceled] + failed_t)
    await _patch_nosleep(monkeypatch)

    with pytest.raises(RuntimeError):
        await orch._wait_for_wave_gate(1)

    halt = next(
        e for e in orch.state.events
        if e["kind"] == "wave_halt_below_soft_green"
    )
    # SAL-3919: denominator = 1 green + 3 failed = 4; ratio = 1/4 = 0.25.
    # Excused=1 (Duplicate) is excluded from the denominator.
    assert halt.get("excused_count") == 1
    assert halt.get("green_ratio") == pytest.approx(1 / 4, abs=1e-3)
    assert halt.get("threshold") == pytest.approx(0.9)
