"""Soul-svc persistence for ``ModelScore`` rows.

Plan M §4.1 emits ``model_scoring.yaml`` for the orchestrator; this module
is the *parallel* path that mirrors the same data into the soul-svc
memory graph under topic ``model_benchmark`` so:

  * the Tiresias scoreboard (Plan M §11.1) can query it cross-deploy,
  * the Aletheia GVR loop (Plan M §1.4) can use it as a verifier prior,
  * Cristian can query "what's the best model for hawkman-qa-a as of last
    Tuesday" without opening the YAML.

One memory record per ``ModelScore``. Content is the JSON-serialised dict
from ``ModelScore.to_dict()``. Topics include:

  * ``model_benchmark`` (always)
  * ``model_benchmark/{persona}`` (so persona-scoped queries are O(1))
  * ``model_benchmark/{task_type}`` (cross-persona task-type queries)
  * ``model_benchmark/model:{model_id}`` (per-model trend lines)

Reads use ``recent_memories`` + topic filter, capped to the soul-svc
v2.0.0 fast-path of ``limit=50`` (memory: ``project_soul_svc_v2_bugs``
hash-collision quirk).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, List, Optional, Sequence

from .scorer import ModelScore


logger = logging.getLogger("alfred_coo.benchmark.storage")


# soul-svc v2.0.0 audit-batch fix put /v1/memory reads at ~6s for limit=200.
# Plan M docs ask for limit=50 to stay under 2s.
DEFAULT_LIMIT = 50


# Topic vocabulary — keep in sync with consumers (scoreboard, Aletheia).
TOPIC_ROOT = "model_benchmark"


def _topics_for(score: ModelScore) -> List[str]:
    """Return the topic set a memory write should carry for this score."""
    return [
        TOPIC_ROOT,
        f"{TOPIC_ROOT}/{score.persona}",
        f"{TOPIC_ROOT}/{score.task_type}",
        f"{TOPIC_ROOT}/model:{score.model}",
    ]


# ── Write path ──────────────────────────────────────────────────────────────


async def store_scores(
    scores: Iterable[ModelScore],
    soul_client: Any,
) -> List[dict]:
    """Persist each score as one memory record.

    Returns the list of soul-svc write responses (each with ``memory_id``
    + ``content_hash``). On individual write failure, logs + continues so
    a single bad row doesn't lose the whole batch.

    ``soul_client`` is duck-typed against ``alfred_coo.soul.SoulClient``:
    we only call ``write_memory(content, topics=...)``. Tests pass a mock.
    """
    responses: List[dict] = []
    for score in scores:
        body = json.dumps(score.to_dict(), sort_keys=True, default=str)
        topics = _topics_for(score)
        try:
            resp = await soul_client.write_memory(content=body, topics=topics)
            responses.append(resp)
            logger.debug(
                "stored score: model=%s persona=%s task_type=%s pass_rate=%.2f memory_id=%s",
                score.model, score.persona, score.task_type, score.pass_rate,
                (resp or {}).get("memory_id") if isinstance(resp, dict) else None,
            )
        except Exception as e:  # noqa: BLE001 — see docstring
            logger.warning(
                "store_scores: write failed for model=%s persona=%s: %s",
                score.model, score.persona, e,
            )
    return responses


# ── Read path ───────────────────────────────────────────────────────────────


async def load_latest_scores(
    soul_client: Any,
    persona: Optional[str] = None,
    task_type: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
) -> List[ModelScore]:
    """Read recent ``model_benchmark`` memories and return one ``ModelScore``
    per (model, persona, task_type) — keeping only the most recent record
    per group.

    The "most recent" is determined by ``last_run`` in the parsed score,
    falling back to memory order (soul-svc returns reverse-chronological).

    ``persona`` / ``task_type`` narrow the topic filter so the read stays
    cheap when the catalogue grows.

    Plan M says limit=50 keeps the read under 2s post-PR-#52; callers
    needing a larger window override it.
    """
    topics = [TOPIC_ROOT]
    if persona:
        topics.append(f"{TOPIC_ROOT}/{persona}")
    if task_type:
        topics.append(f"{TOPIC_ROOT}/{task_type}")

    raw = await soul_client.recent_memories(limit=limit, topics=topics)

    # Most-recent-per-group dedupe.
    by_key: dict[tuple[str, str, str], ModelScore] = {}
    for mem in raw:
        if not isinstance(mem, dict):
            continue
        content = mem.get("content")
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        try:
            score = ModelScore.from_dict(payload)
        except (KeyError, ValueError, TypeError) as e:
            logger.debug("skipping malformed score memory: %s", e)
            continue

        # Apply optional post-filter (in case soul-svc topic-OR vs AND
        # semantics let a non-matching row through).
        if persona is not None and score.persona != persona:
            continue
        if task_type is not None and score.task_type != task_type:
            continue

        key = (score.model, score.persona, score.task_type)
        prev = by_key.get(key)
        if prev is None or score.last_run > prev.last_run:
            by_key[key] = score

    return sorted(
        by_key.values(),
        key=lambda s: (s.persona, s.task_type, s.model),
    )


# ── Convenience: roundtrip helper ───────────────────────────────────────────


async def roundtrip_scores(
    scores: Sequence[ModelScore],
    soul_client: Any,
) -> List[ModelScore]:
    """Store + immediately re-read. Smoke-test helper used by the CLI's
    ``score --persist`` path and the storage integration test.
    """
    await store_scores(scores, soul_client)
    return await load_latest_scores(soul_client)
