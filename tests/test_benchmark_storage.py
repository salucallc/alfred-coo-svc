"""Plan M storage tests.

Mocks the soul-svc client. Verifies:
  1. store_scores writes one memory per ModelScore with correct topics.
  2. store_scores swallows individual write failures (logs + continues).
  3. load_latest_scores parses memory content back into ModelScore.
  4. load_latest_scores keeps only the most-recent record per group.
  5. load_latest_scores's persona / task_type filter is applied.
  6. roundtrip_scores returns the most-recent set.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, List

import pytest

from alfred_coo.benchmark.scorer import ModelScore
from alfred_coo.benchmark.storage import (
    TOPIC_ROOT,
    load_latest_scores,
    roundtrip_scores,
    store_scores,
)


# ── Fake soul client ───────────────────────────────────────────────────────


class _FakeSoulClient:
    """Mock matching the duck-typed surface storage uses:
    ``write_memory(content, topics)`` and ``recent_memories(limit, topics)``.
    """

    def __init__(self, fail_on_models: List[str] | None = None):
        self.writes: List[dict] = []
        self._memory_id = 0
        self._fail_on_models = set(fail_on_models or [])

    async def write_memory(self, content: str, topics: list[str] | None = None):
        # Inspect content to surface the model id; tests can request a
        # particular model's write fail.
        try:
            payload = json.loads(content)
            model = payload.get("model", "")
        except (json.JSONDecodeError, ValueError):
            model = ""
        if model in self._fail_on_models:
            raise RuntimeError(f"forced failure on {model!r}")
        self._memory_id += 1
        rec = {
            "memory_id": f"mem-{self._memory_id}",
            "content_hash": f"hash-{self._memory_id}",
            "content": content,
            "topics": list(topics or []),
        }
        self.writes.append(rec)
        return rec

    async def recent_memories(self, limit: int = 50, topics: list[str] | None = None):
        # Emulate soul-svc topic-OR semantics: a memory matches if any of
        # its topics appear in the requested topics.
        if not topics:
            return list(reversed(self.writes))[:limit]
        wanted = set(topics)
        out = []
        for w in reversed(self.writes):
            if any(t in wanted for t in w.get("topics", [])):
                out.append(w)
            if len(out) >= limit:
                break
        return out

    async def close(self):
        pass


# ── Fixtures ────────────────────────────────────────────────────────────────


def _ms(model, persona="alfred-coo-a", task_type="builder", pass_rate=1.0,
        last_run=None) -> ModelScore:
    return ModelScore(
        model=model,
        persona=persona,
        task_type=task_type,
        pass_rate=pass_rate,
        median_latency_ms=1000,
        median_cost_usd=0.01,
        sample_size=3,
        last_run=last_run or datetime(2026, 4, 29, tzinfo=timezone.utc),
        per_move_pass_rate={},
    )


# ── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_scores_writes_one_per_score():
    client = _FakeSoulClient()
    scores = [
        _ms("m1", persona="alfred-coo-a", task_type="builder"),
        _ms("m2", persona="hawkman-qa-a", task_type="qa"),
    ]
    resps = await store_scores(scores, client)
    assert len(resps) == 2
    assert len(client.writes) == 2

    topics0 = client.writes[0]["topics"]
    assert TOPIC_ROOT in topics0
    assert f"{TOPIC_ROOT}/alfred-coo-a" in topics0
    assert f"{TOPIC_ROOT}/builder" in topics0
    assert f"{TOPIC_ROOT}/model:m1" in topics0


@pytest.mark.asyncio
async def test_store_scores_continues_after_individual_failure():
    client = _FakeSoulClient(fail_on_models=["m-bad"])
    scores = [
        _ms("m-bad"),
        _ms("m-good"),
    ]
    resps = await store_scores(scores, client)
    # Only the good one made it.
    assert len(resps) == 1
    assert len(client.writes) == 1


@pytest.mark.asyncio
async def test_load_latest_scores_parses_back_to_dataclass():
    client = _FakeSoulClient()
    src = [
        _ms("m1", persona="alfred-coo-a", task_type="builder", pass_rate=0.7),
        _ms("m2", persona="hawkman-qa-a", task_type="qa", pass_rate=0.9),
    ]
    await store_scores(src, client)
    loaded = await load_latest_scores(client)
    assert len(loaded) == 2
    by_model = {s.model: s for s in loaded}
    assert by_model["m1"].pass_rate == 0.7
    assert by_model["m2"].persona == "hawkman-qa-a"


@pytest.mark.asyncio
async def test_load_latest_keeps_most_recent_per_group():
    client = _FakeSoulClient()
    older = _ms("m1", pass_rate=0.5, last_run=datetime(2026, 4, 1, tzinfo=timezone.utc))
    newer = _ms("m1", pass_rate=0.9, last_run=datetime(2026, 4, 28, tzinfo=timezone.utc))
    await store_scores([older, newer], client)
    loaded = await load_latest_scores(client)
    assert len(loaded) == 1
    assert loaded[0].pass_rate == 0.9


@pytest.mark.asyncio
async def test_load_latest_persona_filter():
    client = _FakeSoulClient()
    src = [
        _ms("a-build", persona="alfred-coo-a", task_type="builder"),
        _ms("a-qa", persona="hawkman-qa-a", task_type="qa"),
    ]
    await store_scores(src, client)
    out = await load_latest_scores(client, persona="hawkman-qa-a")
    assert len(out) == 1
    assert out[0].persona == "hawkman-qa-a"


@pytest.mark.asyncio
async def test_load_latest_task_type_filter():
    client = _FakeSoulClient()
    src = [
        _ms("a-build", persona="alfred-coo-a", task_type="builder"),
        _ms("a-qa", persona="hawkman-qa-a", task_type="qa"),
    ]
    await store_scores(src, client)
    out = await load_latest_scores(client, task_type="qa")
    assert len(out) == 1
    assert out[0].task_type == "qa"


@pytest.mark.asyncio
async def test_roundtrip_scores():
    client = _FakeSoulClient()
    src = [
        _ms("m1", persona="alfred-coo-a", task_type="builder", pass_rate=0.8),
        _ms("m2", persona="hawkman-qa-a", task_type="qa", pass_rate=0.95),
    ]
    out = await roundtrip_scores(src, client)
    assert {s.model for s in out} == {"m1", "m2"}


@pytest.mark.asyncio
async def test_load_latest_skips_malformed_memory():
    client = _FakeSoulClient()
    # Store one good record then inject a malformed one.
    await store_scores([_ms("m1")], client)
    client.writes.append({
        "memory_id": "bad",
        "content_hash": "x",
        "content": "{not json}",
        "topics": [TOPIC_ROOT],
    })
    out = await load_latest_scores(client)
    # Bad record dropped, good record returned.
    assert len(out) == 1
    assert out[0].model == "m1"
