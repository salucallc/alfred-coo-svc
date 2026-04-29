"""Plan M selector tests.

Covers:
  1. pick_best_model picks highest pass_rate.
  2. Cost is the tiebreaker on equal pass_rate.
  3. Latency is the tiebreaker after cost.
  4. max_cost_usd constraint filters out winners that exceed the cap.
  5. max_latency_ms constraint filters likewise.
  6. NoEligibleModel raises when no row matches the persona.
  7. NoEligibleModel raises when constraints filter the eligible set to empty.
  8. rank_models returns the full sorted list.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from alfred_coo.benchmark.scorer import ModelScore
from alfred_coo.benchmark.selector import (
    NoEligibleModel,
    pick_best_model,
    rank_models,
)


def _ms(model, persona="alfred-coo-a", task_type="builder", pass_rate=1.0,
        cost=0.01, lat=1000, n=3) -> ModelScore:
    return ModelScore(
        model=model,
        persona=persona,
        task_type=task_type,
        pass_rate=pass_rate,
        median_latency_ms=lat,
        median_cost_usd=cost,
        sample_size=n,
        last_run=datetime(2026, 4, 29, tzinfo=timezone.utc),
        per_move_pass_rate={},
    )


def test_pick_picks_highest_pass_rate():
    scores = [
        _ms("a", pass_rate=0.5),
        _ms("b", pass_rate=0.9),
        _ms("c", pass_rate=0.7),
    ]
    assert pick_best_model("alfred-coo-a", "builder", scores) == "b"


def test_pick_breaks_ties_by_cost():
    scores = [
        _ms("a", pass_rate=1.0, cost=0.05),
        _ms("b", pass_rate=1.0, cost=0.01),  # cheapest wins
        _ms("c", pass_rate=1.0, cost=0.02),
    ]
    assert pick_best_model("alfred-coo-a", "builder", scores) == "b"


def test_pick_breaks_cost_ties_by_latency():
    scores = [
        _ms("a", pass_rate=1.0, cost=0.0, lat=2000),
        _ms("b", pass_rate=1.0, cost=0.0, lat=500),  # fastest wins
        _ms("c", pass_rate=1.0, cost=0.0, lat=1000),
    ]
    assert pick_best_model("alfred-coo-a", "builder", scores) == "b"


def test_pick_deterministic_on_full_tie():
    # Same pass_rate, cost, latency → model name asc.
    scores = [
        _ms("z", pass_rate=1.0, cost=0.0, lat=500),
        _ms("a", pass_rate=1.0, cost=0.0, lat=500),
        _ms("m", pass_rate=1.0, cost=0.0, lat=500),
    ]
    assert pick_best_model("alfred-coo-a", "builder", scores) == "a"


def test_pick_filters_by_persona_and_task():
    scores = [
        _ms("hot-qa-model", persona="hawkman-qa-a", task_type="qa", pass_rate=1.0),
        _ms("hot-build-model", persona="alfred-coo-a", task_type="builder", pass_rate=0.6),
    ]
    # Asking for qa picks the hot-qa-model even though hot-build-model has lower pass_rate.
    assert pick_best_model("hawkman-qa-a", "qa", scores) == "hot-qa-model"


def test_pick_max_cost_filters_winner():
    scores = [
        _ms("a", pass_rate=1.0, cost=0.10),  # filtered out by cap
        _ms("b", pass_rate=0.8, cost=0.01),  # second best on rate, but eligible
    ]
    assert pick_best_model(
        "alfred-coo-a", "builder", scores, constraint={"max_cost_usd": 0.05}
    ) == "b"


def test_pick_max_latency_filters_winner():
    scores = [
        _ms("a", pass_rate=1.0, lat=10_000),
        _ms("b", pass_rate=0.7, lat=500),
    ]
    assert pick_best_model(
        "alfred-coo-a", "builder", scores, constraint={"max_latency_ms": 5_000}
    ) == "b"


def test_pick_min_pass_rate_filters():
    scores = [
        _ms("a", pass_rate=0.6),
        _ms("b", pass_rate=0.3),
    ]
    # Min 0.5 → only a is eligible.
    assert pick_best_model(
        "alfred-coo-a", "builder", scores, constraint={"min_pass_rate": 0.5}
    ) == "a"


def test_pick_raises_when_no_persona_match():
    scores = [_ms("a", persona="other", pass_rate=1.0)]
    with pytest.raises(NoEligibleModel) as exc:
        pick_best_model("alfred-coo-a", "builder", scores)
    assert exc.value.persona == "alfred-coo-a"
    assert exc.value.task_type == "builder"


def test_pick_raises_when_constraint_unsatisfiable():
    scores = [_ms("a", pass_rate=1.0, cost=0.10)]
    with pytest.raises(NoEligibleModel) as exc:
        pick_best_model(
            "alfred-coo-a", "builder", scores, constraint={"max_cost_usd": 0.01}
        )
    assert "a" in exc.value.tried


def test_rank_models_returns_full_sort():
    scores = [
        _ms("a", pass_rate=0.5, cost=0.01),
        _ms("b", pass_rate=1.0, cost=0.05),
        _ms("c", pass_rate=1.0, cost=0.01),
    ]
    ranked = rank_models("alfred-coo-a", "builder", scores)
    assert [s.model for s in ranked] == ["c", "b", "a"]


def test_unknown_constraint_keys_ignored():
    scores = [_ms("a", pass_rate=1.0)]
    # An unknown constraint key should be silently dropped, not raise.
    out = pick_best_model(
        "alfred-coo-a", "builder", scores, constraint={"max_unicorns": 0}
    )
    assert out == "a"
