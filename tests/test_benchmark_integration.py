"""Plan M end-to-end integration tests.

Wires the runner (with a fake dispatcher), the scorer, the selector, and
the storage layer (with the fake soul client) into one flow. This is the
"can a developer go from fixture run to score-driven model pick without
touching live infra" smoke.

Three flows exercised:

  1. fake-run → aggregate → store → load → pick
     End-to-end: 3 models × 1 fixture, where one model is clearly better.
     selector.pick_best_model should return the better model.

  2. real evaluator end-to-end on a transcript_assert fixture
     Uses the M-MV-01 fixture and a hand-built passing transcript to
     verify the runner.evaluate() integrates correctly with scorer.aggregate().

  3. soul-svc unavailable → cli scoring degrades cleanly
     storage.store_scores's failure path was tested in unit; here we
     verify aggregate still produces ModelScores when no soul is wired.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List

import pytest

from alfred_coo.benchmark.runner import evaluate, run_fixture
from alfred_coo.benchmark.schema import (
    Fixture,
    PassCriterion,
    RunResult,
    Score,
    ToolScriptEntry,
    load_fixture,
)
from alfred_coo.benchmark.scorer import aggregate
from alfred_coo.benchmark.selector import pick_best_model
from alfred_coo.benchmark.storage import load_latest_scores, store_scores


# ── Fakes ───────────────────────────────────────────────────────────────────


class _FakeSoul:
    def __init__(self):
        self.writes: list[dict] = []

    async def write_memory(self, content, topics=None):
        rec = {"content": content, "topics": list(topics or []), "memory_id": f"m-{len(self.writes)}"}
        self.writes.append(rec)
        return rec

    async def recent_memories(self, limit=50, topics=None):
        wanted = set(topics or [])
        if not wanted:
            return list(reversed(self.writes))[:limit]
        out = []
        for w in reversed(self.writes):
            if any(t in wanted for t in w.get("topics", [])):
                out.append(w)
        return out[:limit]

    async def close(self):
        pass


@dataclass
class _ScriptedDispatch:
    """Fake Dispatcher: matches the call_with_tools signature we use and
    returns a canned transcript per model id.
    """
    transcripts: Dict[str, Dict[str, Any]]

    async def call_with_tools(self, model, system, prompt, tools, context):
        # Return the canned transcript for this model.
        t = self.transcripts.get(model)
        if t is None:
            return {"content": "", "tool_calls": []}
        return dict(t)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_fixture(move_id: str, persona: str, *, want_tool="propose_pr") -> Fixture:
    """Tiny in-memory fixture: must call ``want_tool`` to pass."""
    return Fixture(
        move_id=move_id,
        name=f"name-{move_id}",
        source="integration-test",
        persona_id=persona,
        resolved_prompt_sha="sha256:test",
        tool_allowlist=[want_tool],
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u"},
        ],
        tool_script=[
            ToolScriptEntry(tool=want_tool, args_match={}, return_value={"ok": True}),
        ],
        turn_limit=4,
        pass_criterion=PassCriterion(
            kind="transcript_assert",
            spec={"asserts": [{"must": {"tool_call": want_tool}}]},
        ),
        tags=["builder"],
    )


# ── Test 1: end-to-end best-model flow ─────────────────────────────────────


@pytest.mark.asyncio
async def test_end_to_end_run_aggregate_store_pick():
    fixture = _make_fixture("M-INT-01", "alfred-coo-a", want_tool="propose_pr")

    transcripts = {
        "good-model": {
            "content": "All done.",
            "tool_calls": [
                {"iteration": 0, "name": "propose_pr",
                 "arguments": json.dumps({"branch": "feature/sal-x"}), "result": "{}"},
            ],
        },
        "bad-model": {
            "content": "I think this is unclear, escalating.",
            "tool_calls": [],
        },
        "mid-model": {
            "content": "Check the diff.",
            "tool_calls": [
                {"iteration": 0, "name": "propose_pr",
                 "arguments": json.dumps({"branch": "feature/sal-y"}), "result": "{}"},
            ],
        },
    }
    dispatcher = _ScriptedDispatch(transcripts=transcripts)

    results: List[RunResult] = []
    for model in transcripts:
        rr = await run_fixture(
            fixture, model, n_samples=2, dispatcher=dispatcher,
        )
        results.append(rr)

    scores = aggregate(results, [fixture])
    # Three models × one persona × one task_type → three score rows.
    assert len(scores) == 3
    by_model = {s.model: s for s in scores}
    assert by_model["good-model"].pass_rate == 1.0
    assert by_model["bad-model"].pass_rate == 0.0
    assert by_model["mid-model"].pass_rate == 1.0  # also passes (it called propose_pr)

    # Persist + reload through the fake soul client.
    soul = _FakeSoul()
    await store_scores(scores, soul)
    loaded = await load_latest_scores(soul)
    assert len(loaded) == 3

    # Selector should prefer good-model or mid-model (tie 1.0 pass_rate);
    # tiebreakers will pick deterministically. Both must NOT be bad-model.
    best = pick_best_model("alfred-coo-a", "builder", loaded)
    assert best != "bad-model"


# ── Test 2: real evaluator on canonical fixture ─────────────────────────────


def test_real_evaluator_on_M_MV_01_passing_transcript():
    fixture = load_fixture("M-MV-01")
    transcript = {
        "content": (
            "Confirmed docker-compose.yml at deploy/appliance/docker-compose.yml. "
            "Opened PR with the env-var block update."
        ),
        "tool_calls": [
            {"iteration": 0, "name": "http_get",
             "arguments": json.dumps({"url": "https://api.github.com/repos/salucallc/alfred-coo-svc/contents/deploy/appliance/docker-compose.yml?ref=main"}),
             "result": "{}"},
            {"iteration": 1, "name": "propose_pr",
             "arguments": json.dumps({"branch": "feature/sal-2634-compose-env"}),
             "result": "{}"},
        ],
    }
    score = evaluate(fixture, transcript)
    assert score.passed, f"expected pass, got {score.reasons!r}"


# ── Test 3: graceful degradation when no soul ──────────────────────────────


@pytest.mark.asyncio
async def test_aggregate_works_without_storage():
    fixture = _make_fixture("M-INT-02", "alfred-coo-a", want_tool="propose_pr")
    transcripts = {
        "m1": {"content": "", "tool_calls": [
            {"iteration": 0, "name": "propose_pr",
             "arguments": json.dumps({}), "result": "{}"}
        ]},
    }
    dispatcher = _ScriptedDispatch(transcripts=transcripts)
    rr = await run_fixture(fixture, "m1", n_samples=2, dispatcher=dispatcher)
    scores = aggregate([rr], [fixture])
    assert len(scores) == 1
    assert scores[0].pass_rate == 1.0
    # Selector works on the in-memory list without ever touching soul.
    assert pick_best_model("alfred-coo-a", "builder", scores) == "m1"


# ── Test 4: criterion library check (callable_import-style) ────────────────


def test_structured_emit_criterion_real_check():
    """Plan M structured_emit kind exercised end-to-end: a passing transcript
    where the propose_pr branch arg matches the regex, and a failing one
    where it does not. This is the criterion-library check the user spec
    asked for as the integration anchor.
    """
    fixture = Fixture(
        move_id="M-INT-SE",
        name="structured-emit-anchor",
        source="integration test",
        persona_id="alfred-coo-a",
        resolved_prompt_sha="sha256:test",
        tool_allowlist=["propose_pr"],
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u"},
        ],
        tool_script=[],
        turn_limit=2,
        pass_criterion=PassCriterion(
            kind="structured_emit",
            spec={
                "tool": "propose_pr",
                "arg_regexes": {"branch": r"^feature/sal-\d+"},
                "min_calls": 1,
                "max_calls": 1,
            },
        ),
        tags=["builder"],
    )

    good = {
        "content": "",
        "tool_calls": [
            {"iteration": 0, "name": "propose_pr",
             "arguments": json.dumps({"branch": "feature/sal-2634-x"}),
             "result": "{}"},
        ],
    }
    bad = {
        "content": "",
        "tool_calls": [
            {"iteration": 0, "name": "propose_pr",
             "arguments": json.dumps({"branch": "main"}),
             "result": "{}"},
        ],
    }
    assert evaluate(fixture, good).passed is True
    score_bad = evaluate(fixture, bad)
    assert score_bad.passed is False
    assert any("branch" in r for r in score_bad.reasons)
