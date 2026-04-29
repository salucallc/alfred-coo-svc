"""Plan M scorer tests.

Covers:
  1. aggregate() groups by (model, persona, task_type) correctly.
  2. pass_rate is the binary mean of per-sample passes.
  3. median_latency_ms / median_cost_usd reach into Score.details correctly.
  4. per_move_pass_rate sub-map is populated.
  5. task_type_for() uses tag-first, then heuristic.
  6. ModelScore.from_dict / to_dict roundtrip is lossless.
"""

from __future__ import annotations

from datetime import datetime, timezone

from alfred_coo.benchmark.schema import Fixture, PassCriterion, RunResult, Score
from alfred_coo.benchmark.scorer import ModelScore, aggregate, task_type_for


# ── Helpers ─────────────────────────────────────────────────────────────────


def _fix(move_id: str, persona: str, tags=None) -> Fixture:
    return Fixture(
        move_id=move_id,
        name=f"name-{move_id}",
        source="test",
        persona_id=persona,
        resolved_prompt_sha="sha256:test",
        tool_allowlist=[],
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u"},
        ],
        tool_script=[],
        turn_limit=4,
        pass_criterion=PassCriterion(kind="transcript_assert", spec={"asserts": []}),
        tags=list(tags or []),
    )


def _result(move_id: str, model: str, verdicts: list[bool], lat=None, cost=None) -> RunResult:
    samples = []
    for i, v in enumerate(verdicts):
        details = {"run_id": f"r-{i}"}
        if lat is not None:
            details["latency_ms"] = lat[i] if isinstance(lat, list) else lat
        if cost is not None:
            details["cost_usd"] = cost[i] if isinstance(cost, list) else cost
        samples.append(Score(passed=v, reasons=[] if v else ["fail"], details=details))
    return RunResult(move_id=move_id, model=model, samples=samples, trace_ids=[])


# ── Tests ───────────────────────────────────────────────────────────────────


def test_task_type_for_uses_tag_first():
    f = _fix("M-MV-99", "alfred-coo-a", tags=["qa", "extra"])
    assert task_type_for(f) == "qa"


def test_task_type_for_falls_back_to_persona():
    assert task_type_for(_fix("M-MV-1", "hawkman-qa-a")) == "qa"
    assert task_type_for(_fix("M-MV-2", "alfred-coo-a")) == "orchestrator"
    assert task_type_for(_fix("M-MV-3", "autonomous-build-a")) == "builder"
    assert task_type_for(_fix("M-MV-4", "weird-persona")) == "builder"


def test_aggregate_groups_by_model_persona_task():
    fixtures = [
        _fix("M-MV-A", "alfred-coo-a", tags=["builder"]),
        _fix("M-MV-B", "alfred-coo-a", tags=["builder"]),
        _fix("M-MV-C", "hawkman-qa-a", tags=["qa"]),
    ]
    results = [
        _result("M-MV-A", "model-x", [True, True, False]),  # 2/3
        _result("M-MV-B", "model-x", [True, True, True]),   # 3/3
        _result("M-MV-A", "model-y", [False, False, False]), # 0/3
        _result("M-MV-C", "model-x", [True, True]),         # 2/2 (qa)
    ]
    scores = aggregate(results, fixtures)

    by_key = {(s.model, s.persona, s.task_type): s for s in scores}
    assert (
        "model-x", "alfred-coo-a", "builder",
    ) in by_key
    s = by_key[("model-x", "alfred-coo-a", "builder")]
    # 2 + 3 passes of 6 samples = 5/6
    assert s.sample_size == 6
    assert s.pass_rate == round(5 / 6, 4)
    # per-move breakdown
    assert s.per_move_pass_rate["M-MV-A"] == round(2 / 3, 4)
    assert s.per_move_pass_rate["M-MV-B"] == 1.0

    # qa group separate from builder
    qa = by_key[("model-x", "hawkman-qa-a", "qa")]
    assert qa.pass_rate == 1.0
    assert qa.sample_size == 2


def test_aggregate_median_latency_and_cost():
    fixtures = [_fix("M-MV-A", "alfred-coo-a", tags=["builder"])]
    # latencies = [100, 200, 300] → median 200; costs = [0.01, 0.02, 0.03] → median 0.02
    results = [
        _result(
            "M-MV-A", "m1", [True, True, True],
            lat=[100, 200, 300], cost=[0.01, 0.02, 0.03],
        )
    ]
    scores = aggregate(results, fixtures)
    assert len(scores) == 1
    assert scores[0].median_latency_ms == 200
    assert abs(scores[0].median_cost_usd - 0.02) < 1e-9


def test_aggregate_default_when_no_latency_in_details():
    fixtures = [_fix("M-MV-A", "alfred-coo-a", tags=["builder"])]
    results = [_result("M-MV-A", "m1", [True])]
    scores = aggregate(results, fixtures)
    assert scores[0].median_latency_ms == 0
    assert scores[0].median_cost_usd == 0.0


def test_aggregate_unknown_fixture_falls_back_to_other():
    # No fixtures supplied → persona unknown, task_type "other"
    results = [_result("M-MV-Z", "m1", [True])]
    scores = aggregate(results, fixtures=None)
    assert len(scores) == 1
    assert scores[0].persona == "?"
    assert scores[0].task_type == "other"


def test_model_score_roundtrip():
    s = ModelScore(
        model="m1",
        persona="alfred-coo-a",
        task_type="builder",
        pass_rate=0.75,
        median_latency_ms=1234,
        median_cost_usd=0.0042,
        sample_size=12,
        last_run=datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc),
        per_move_pass_rate={"M-MV-A": 1.0, "M-MV-B": 0.5},
    )
    raw = s.to_dict()
    s2 = ModelScore.from_dict(raw)
    assert s2.model == s.model
    assert s2.persona == s.persona
    assert s2.task_type == s.task_type
    assert s2.pass_rate == s.pass_rate
    assert s2.median_latency_ms == s.median_latency_ms
    assert abs(s2.median_cost_usd - s.median_cost_usd) < 1e-9
    assert s2.sample_size == s.sample_size
    assert s2.last_run == s.last_run
    assert s2.per_move_pass_rate == s.per_move_pass_rate


def test_aggregate_sorted_output():
    fixtures = [
        _fix("M-MV-A", "alfred-coo-a", tags=["builder"]),
        _fix("M-MV-B", "hawkman-qa-a", tags=["qa"]),
    ]
    results = [
        _result("M-MV-A", "z-model", [True]),
        _result("M-MV-B", "a-model", [True]),
        _result("M-MV-A", "a-model", [True]),
    ]
    scores = aggregate(results, fixtures)
    # Sort key: (persona, task_type, model)
    keys = [(s.persona, s.task_type, s.model) for s in scores]
    assert keys == sorted(keys)
