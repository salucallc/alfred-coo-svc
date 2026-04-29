"""Per-(model, persona, move) score aggregator.

Plan M §3.5 + §4.1: turn a list of ``RunResult`` rows into a list of
``ModelScore`` rows ready for ``model_scoring.yaml`` emit / soul-svc
storage / ``selector.pick_best_model`` consumption.

This is the M-06 slice that bridges the runner's per-fixture binary verdict
to the selector's per-persona aggregate decision. It is intentionally
boring: median + ratio + count over a list. Anything fancier (weighted
aggregates per move, per-persona min-aggregate floors) lives in the
selector and the YAML emitter where it can be tuned without re-running
the benchmark.

Aggregation grouping key: ``(model, persona_id, task_type)``.

* ``model``       — the candidate model id, e.g. ``gpt-oss:120b-cloud``.
* ``persona_id``  — the persona under test, e.g. ``alfred-coo-a``.
* ``task_type``   — coarse task class extracted from ``Fixture.tags`` or
  defaulted from the persona id (see ``task_type_for``). Three values v1.0:
  ``builder``, ``qa``, ``orchestrator``. Plus a catch-all ``other`` so an
  uncategorised fixture aggregates somewhere instead of dropping.

Latency + cost are NOT recorded by the v1.0 runner (Plan M §7 explicitly
defers cost-per-move accounting). The scorer accepts them when supplied
via ``Score.details`` (keys ``latency_ms``, ``cost_usd``) and emits zero
otherwise. M-07 wiring writes these from proxy trace rows.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .schema import Fixture, RunResult, Score


# ── Public dataclass ────────────────────────────────────────────────────────


@dataclass
class ModelScore:
    """Aggregate per-(model, persona, task_type) verdict.

    Mirrors the ``models:`` block emitted into ``model_scoring.yaml``
    (Plan M §4.1) plus the trailing latency / cost stats that the
    selector's optional cost / latency constraints read.

    ``pass_rate`` is binary by construction: average of per-sample 0/1
    verdicts. v1.1 graded scoring will re-shape this; until then,
    ``sum(passes) / sample_size`` is exact.
    """

    model: str
    persona: str
    task_type: str
    pass_rate: float
    median_latency_ms: int
    median_cost_usd: float
    sample_size: int
    last_run: datetime
    # Optional move-id breakdown so the selector can show "this model
    # passes M-MV-01 but fails M-MV-02" without re-aggregating from
    # raw RunResults. Keys are move_ids; values are pass rates 0..1.
    per_move_pass_rate: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "persona": self.persona,
            "task_type": self.task_type,
            "pass_rate": self.pass_rate,
            "median_latency_ms": self.median_latency_ms,
            "median_cost_usd": self.median_cost_usd,
            "sample_size": self.sample_size,
            "last_run": self.last_run.isoformat(),
            "per_move_pass_rate": dict(self.per_move_pass_rate),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ModelScore":
        last_run_raw = raw.get("last_run")
        if isinstance(last_run_raw, datetime):
            last_run = last_run_raw
        elif isinstance(last_run_raw, str):
            last_run = datetime.fromisoformat(last_run_raw)
        else:
            last_run = datetime.now(timezone.utc)
        return cls(
            model=str(raw["model"]),
            persona=str(raw["persona"]),
            task_type=str(raw["task_type"]),
            pass_rate=float(raw.get("pass_rate", 0.0)),
            median_latency_ms=int(raw.get("median_latency_ms", 0)),
            median_cost_usd=float(raw.get("median_cost_usd", 0.0)),
            sample_size=int(raw.get("sample_size", 0)),
            last_run=last_run,
            per_move_pass_rate={
                str(k): float(v) for k, v in (raw.get("per_move_pass_rate") or {}).items()
            },
        )


# ── Helpers ─────────────────────────────────────────────────────────────────


# Persona-id → task-type heuristic. Override per fixture by adding one of
# {builder, qa, orchestrator, kickoff, decompose, review} to ``Fixture.tags``;
# the first hit wins. Plan M §3.1 keeps tags free-form so this stays cheap.
_TASK_TYPE_TAGS = {"builder", "qa", "kickoff", "decompose", "review", "orchestrator"}


def task_type_for(fixture: Fixture) -> str:
    """Return the task_type bucket for a fixture.

    Order of precedence:
      1. First tag in ``fixture.tags`` that names a known task type.
      2. Persona-name heuristic: ``hawkman`` / ``-qa-`` → ``qa``,
         ``alfred-coo`` / ``-coo-`` / ``orchestrator`` → ``orchestrator``,
         everything else → ``builder``.
    """
    for tag in fixture.tags:
        if tag in _TASK_TYPE_TAGS:
            return tag
    pid = fixture.persona_id.lower()
    if "hawkman" in pid or "-qa-" in pid or pid.endswith("-qa"):
        return "qa"
    if "coo" in pid or "orchestrator" in pid:
        return "orchestrator"
    if "autonomous-build" in pid or pid.endswith("-builder"):
        return "builder"
    return "builder"


def _stat_from_samples(samples: Sequence[Score], key: str, *, as_int: bool = False) -> Any:
    """Pull a numeric stat (latency_ms, cost_usd) from per-sample details
    and return its median. Missing values count as 0.

    Returns int when ``as_int`` is True, else float.
    """
    vals: List[float] = []
    for s in samples:
        v = s.details.get(key) if s.details else None
        if isinstance(v, (int, float)):
            vals.append(float(v))
        else:
            vals.append(0.0)
    if not vals:
        return 0 if as_int else 0.0
    med = statistics.median(vals)
    return int(med) if as_int else float(med)


# ── Aggregation ─────────────────────────────────────────────────────────────


# Group key: (model, persona, task_type). Move-id breakdowns roll up under
# this key into the per_move_pass_rate sub-map.
_GroupKey = Tuple[str, str, str]


def aggregate(
    results: Iterable[RunResult],
    fixtures: Optional[Iterable[Fixture]] = None,
    *,
    now: Optional[datetime] = None,
) -> List[ModelScore]:
    """Group ``results`` by (model, persona, task_type) and compute
    per-group ``ModelScore``.

    Parameters
    ----------
    results : Iterable[RunResult]
        The runner output. Each row is one (model, fixture) pair with N
        per-sample ``Score`` entries.
    fixtures : Iterable[Fixture] | None
        The fixture catalogue. Used to look up persona_id + task_type by
        move_id. When None, persona is inferred from the move-id prefix
        (``M-MV-*`` → unknown persona ``"?"``) — callers should normally
        pass the catalogue.
    now : datetime | None
        Override the ``last_run`` timestamp. Default: ``datetime.now(UTC)``.

    Returns
    -------
    list[ModelScore]
        One row per (model, persona, task_type) group. Sorted by
        (persona, task_type, model) for stable output.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Build move_id → (persona_id, task_type) lookup.
    fixture_index: Dict[str, Tuple[str, str]] = {}
    if fixtures is not None:
        for f in fixtures:
            fixture_index[f.move_id] = (f.persona_id, task_type_for(f))

    # First pass: group raw samples by group_key + move_id.
    grouped_samples: Dict[_GroupKey, List[Score]] = {}
    grouped_per_move: Dict[_GroupKey, Dict[str, List[Score]]] = {}

    for result in results:
        persona, task_type = fixture_index.get(result.move_id, ("?", "other"))
        key: _GroupKey = (result.model, persona, task_type)
        grouped_samples.setdefault(key, []).extend(result.samples)
        per_move = grouped_per_move.setdefault(key, {})
        per_move.setdefault(result.move_id, []).extend(result.samples)

    # Second pass: compute the ModelScore per group.
    out: List[ModelScore] = []
    for key, samples in grouped_samples.items():
        model, persona, task_type = key
        if not samples:
            continue
        passes = sum(1 for s in samples if s.passed)
        pass_rate = round(passes / len(samples), 4)
        per_move_rates = {}
        for move_id, msamples in grouped_per_move[key].items():
            mp = sum(1 for s in msamples if s.passed)
            per_move_rates[move_id] = round(mp / len(msamples), 4) if msamples else 0.0
        out.append(
            ModelScore(
                model=model,
                persona=persona,
                task_type=task_type,
                pass_rate=pass_rate,
                median_latency_ms=_stat_from_samples(samples, "latency_ms", as_int=True),
                median_cost_usd=_stat_from_samples(samples, "cost_usd"),
                sample_size=len(samples),
                last_run=now,
                per_move_pass_rate=per_move_rates,
            )
        )

    out.sort(key=lambda s: (s.persona, s.task_type, s.model))
    return out
