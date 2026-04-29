"""``pick_best_model`` — the selector that consumes ``ModelScore`` rows
and returns the model id ``dispatch.select_model`` should route to.

Plan M §4.2 contract: ``select_model`` checks per-persona signature-move
floors; this selector is the simpler "best by pass_rate" picker that
``model_registry.USE_BENCHMARK_SCORES`` flag flips on.

Default policy (Plan M §3.4 binary scoring):

  1. Filter ``scores`` to rows matching the requested ``persona`` and
     ``task_type``.
  2. Drop rows that violate any constraint in ``constraint`` (e.g.
     ``max_cost_usd``, ``max_latency_ms``).
  3. Sort the remainder by:
        - ``pass_rate`` desc
        - ``median_cost_usd`` asc (tiebreaker: cheaper wins)
        - ``median_latency_ms`` asc (final tiebreaker)
        - ``model`` asc (deterministic last-resort)
  4. Return the top model id.

If filtering removes everything (no model meets the constraints, or no
data exists for this persona/task_type), the function raises
``NoEligibleModel``. The caller (``model_registry``) catches and falls
back to the static fallback chain.

The selector does not know about persona signature-move floors yet; that
is the M-06 ``ModelScoring.eligible()`` API (Plan M §4.2). v1.0 selector
optimises pass_rate over the filtered set; the per-persona floor lands
when ``model_scoring.yaml`` does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .scorer import ModelScore


# ── Exceptions ──────────────────────────────────────────────────────────────


class NoEligibleModel(RuntimeError):
    """Raised when ``pick_best_model`` cannot return a model.

    The caller should fall back to its static selection path. Carries the
    persona / task_type / constraint that produced the empty set so the
    log message in ``model_registry`` is actionable.
    """

    def __init__(
        self,
        persona: str,
        task_type: str,
        constraint: Optional[Mapping[str, Any]] = None,
        tried: Optional[Sequence[str]] = None,
    ):
        self.persona = persona
        self.task_type = task_type
        self.constraint = dict(constraint or {})
        self.tried = list(tried or [])
        msg = (
            f"no eligible model for persona={persona!r} task_type={task_type!r} "
            f"constraint={self.constraint!r} (tried={self.tried!r})"
        )
        super().__init__(msg)


# ── Constraint vocabulary ──────────────────────────────────────────────────


# Each constraint key maps to a (ModelScore -> bool) predicate. Add new
# keys here; the selector ignores unknown keys with no error so the
# constraint dict is forward-compatible.
def _make_predicates(constraint: Mapping[str, Any]) -> List[tuple[str, Any]]:
    """Validate + normalise a constraint dict into a list of
    (key, threshold) pairs. Unknown keys are dropped (logged at the call
    site if desired)."""
    out: List[tuple[str, Any]] = []
    for k, v in constraint.items():
        if k in {"max_cost_usd", "max_latency_ms", "min_pass_rate", "min_sample_size"}:
            out.append((k, v))
    return out


def _meets(score: ModelScore, key: str, threshold: Any) -> bool:
    if key == "max_cost_usd":
        return score.median_cost_usd <= float(threshold)
    if key == "max_latency_ms":
        return score.median_latency_ms <= int(threshold)
    if key == "min_pass_rate":
        return score.pass_rate >= float(threshold)
    if key == "min_sample_size":
        return score.sample_size >= int(threshold)
    return True  # unknown key — vacuously satisfied


# ── Public API ──────────────────────────────────────────────────────────────


def pick_best_model(
    persona: str,
    task_type: str,
    scores: Iterable[ModelScore],
    constraint: Optional[Mapping[str, Any]] = None,
) -> str:
    """Return the model id with the highest pass_rate for this persona+task,
    breaking ties by cost then latency. See module docstring for full
    policy.

    Raises ``NoEligibleModel`` when no rows match the filters or the
    constraint set is unsatisfiable.
    """
    constraint = constraint or {}
    predicates = _make_predicates(constraint)

    filtered: List[ModelScore] = []
    tried: List[str] = []
    for s in scores:
        if s.persona != persona or s.task_type != task_type:
            continue
        tried.append(s.model)
        if all(_meets(s, k, v) for k, v in predicates):
            filtered.append(s)

    if not filtered:
        raise NoEligibleModel(
            persona=persona, task_type=task_type, constraint=constraint, tried=tried
        )

    filtered.sort(
        key=lambda s: (
            -s.pass_rate,            # desc
            s.median_cost_usd,       # asc
            s.median_latency_ms,     # asc
            s.model,                 # asc (deterministic)
        )
    )
    return filtered[0].model


def rank_models(
    persona: str,
    task_type: str,
    scores: Iterable[ModelScore],
    constraint: Optional[Mapping[str, Any]] = None,
) -> List[ModelScore]:
    """Return the full sorted list of eligible ModelScore rows for this
    persona+task. Useful for logging / explainability ("we picked X
    because Y was Δ pass_rate behind").
    """
    constraint = constraint or {}
    predicates = _make_predicates(constraint)
    out = [
        s for s in scores
        if s.persona == persona
        and s.task_type == task_type
        and all(_meets(s, k, v) for k, v in predicates)
    ]
    out.sort(
        key=lambda s: (
            -s.pass_rate,
            s.median_cost_usd,
            s.median_latency_ms,
            s.model,
        )
    )
    return out
