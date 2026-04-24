"""Plan M persona-move benchmark substrate.

This package ships the fixture schema, canonical fixtures, runner, evaluator,
and CLI that let us score a candidate model against a persona's signature
moves BEFORE dispatching live work to it.

Plan reference: Z:/_planning/v1-ga/M_model_benchmark_substrate.md

Launch scope (M-01 + M-02):
  * ``schema.py`` — Fixture / Score / RunResult dataclasses + JSON parser +
    ``compute_prompt_sha`` helper for persona-drift detection.
  * ``runner.py`` — ``FixtureScript`` tool-call interceptor,
    ``evaluate()`` binary transcript evaluator, ``run_fixture()`` dispatcher.
  * ``fixtures/`` — 3 canonical fixtures (M-MV-01, M-MV-02, M-MV-06).
  * ``cli.py`` — ``python -m alfred_coo.benchmark run --move X --model Y``.

Deferred to later M-* tickets:
  * ``model_scoring.yaml`` emitter + ``ModelScoring`` loader (M-06).
  * ``select_model`` contract change (M-06).
  * Remaining 8 fixtures (M-05).
  * ``benchmark-svc`` compose service + ``score`` / ``report`` / ``watch``
    subcommands (M-03 / M-07).
  * Trace-column migration + retention override (M-04).
  * Import-linter seam (M-08).
"""

__version__ = "0.1.0"
