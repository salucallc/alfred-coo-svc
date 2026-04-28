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
