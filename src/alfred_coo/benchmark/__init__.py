"""Plan M persona-move benchmark substrate.

This package ships the fixture schema, canonical fixtures, runner, evaluator,
scorer, selector, soul-svc storage, and CLI that let us score a candidate
model against a persona's signature moves BEFORE dispatching live work.

Plan reference: Z:/_planning/v1-ga/M_model_benchmark_substrate.md

Public surface:
  * Schema:    Fixture, PassCriterion, ToolScriptEntry, Score, RunResult,
               compute_prompt_sha, load_fixture, load_all_fixtures.
  * Runner:    FixtureScript, UnscriptedToolCall, evaluate, run_fixture.
  * Scorer:    ModelScore, aggregate, task_type_for.
  * Selector:  pick_best_model, rank_models, NoEligibleModel.
  * Storage:   store_scores, load_latest_scores, roundtrip_scores.

Launch slice covered by this package (M-01 + M-02 + scorer/selector/storage
slice of M-06):
  * Fixture schema + canonical persona-move fixtures (M-MV-01..M-MV-06).
  * Tool-script interceptor + transcript_assert / terminal_form /
    structured_emit evaluators.
  * Scorer aggregating per-(model, persona, task_type) pass rates.
  * Selector picking best model with optional cost / latency constraints.
  * Soul-svc round-trip storage of ModelScores for cross-deploy lookup.
  * CLI subcommand: ``run`` (single fixture × single model).

Deferred to later M-* tickets (still tracked in Plan M):
  * model_scoring.yaml emitter (M-06 Phase 2).
  * dispatch.select_model contract change with NoEligibleModel raise (M-06).
  * benchmark-svc compose service (M-03).
  * Trace-column migration + 90d retention (M-04).
  * Nightly cron + scoreboard markdown report (M-07).
  * Import-linter seam (M-08).
"""

__version__ = "0.2.0"

from .runner import FixtureScript, UnscriptedToolCall, evaluate, run_fixture
from .schema import (
    FIXTURE_SCHEMA_VERSION,
    Fixture,
    PassCriterion,
    RunResult,
    Score,
    ToolScriptEntry,
    compute_prompt_sha,
    fixture_dir,
    load_all_fixtures,
    load_fixture,
)
from .scorer import ModelScore, aggregate, task_type_for
from .selector import NoEligibleModel, pick_best_model, rank_models
from .storage import (
    DEFAULT_LIMIT,
    TOPIC_ROOT,
    load_latest_scores,
    roundtrip_scores,
    store_scores,
)

__all__ = [
    # schema
    "FIXTURE_SCHEMA_VERSION",
    "Fixture",
    "PassCriterion",
    "RunResult",
    "Score",
    "ToolScriptEntry",
    "compute_prompt_sha",
    "fixture_dir",
    "load_all_fixtures",
    "load_fixture",
    # runner
    "FixtureScript",
    "UnscriptedToolCall",
    "evaluate",
    "run_fixture",
    # scorer
    "ModelScore",
    "aggregate",
    "task_type_for",
    # selector
    "NoEligibleModel",
    "pick_best_model",
    "rank_models",
    # storage
    "DEFAULT_LIMIT",
    "TOPIC_ROOT",
    "load_latest_scores",
    "roundtrip_scores",
    "store_scores",
]
