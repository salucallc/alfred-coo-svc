# AD-f: Hypothesis tree + recursion (depth-4 cap)

**Parent:** alfred-doctor epic — see `plans/v1-ga/AD.md`
**Linear:** [SAL-3286](https://linear.app/saluca/issue/SAL-3286)
**Wave:** wave-3

## Context

Step 4 of the architectural skeleton. Each `Surprise` from the investigation loop (AD-d) seeds a hypothesis tree. Each hypothesis has `confidence_score`, `supporting_evidence`, `counter_evidence`. Children are formed by asking: "If H is true, what would we expect to see? VERIFY." Recursion bottoms out either when a leaf is byte-verified by `state.db` data (root-cause found) or at depth 4 (cost guardrail).

Verification at the leaf level is byte-level: the node carries an evidence-query (e.g. SQL against `state.db`, or a substring check against journal output) and the verifier executes it.

## Target paths

* `src/alfred_doctor/hypothesis.py`
* `src/alfred_doctor/verify.py`
* `tests/test_hypothesis.py`
* `plans/v1-ga/AD-f.md`

## Dependencies

Upstream: AD-a (verifier reads from `state.db`), AD-d (consumes `Surprise` seeds).
Downstream: AD-g (logs tree outcomes for learning), AD-h (renders tree on dashboard).

## APE/V Acceptance

**A — Action:**

1. Add `src/alfred_doctor/hypothesis.py` with dataclass `Hypothesis(id, statement, confidence, supporting_evidence, counter_evidence, parent_id, depth, evidence_query, status)` where `status ∈ {"open", "verified_true", "verified_false", "depth_capped"}`.
2. Add class `HypothesisTree` with methods `seed(surprise) -> Hypothesis` (returns root) and `expand(node) -> list[Hypothesis]` (returns children, capped so `depth <= 4`).
3. Add `src/alfred_doctor/verify.py` with function `verify(node) -> Literal["true", "false", "inconclusive"]` that runs `node.evidence_query` against `state.db` (SQL) or against the relevant ingest event (substring match). Inconclusive results leave `status="open"`.
4. Add walker `HypothesisTree.run_to_root_cause(seed) -> RootCauseResult` that BFS-expands until either: (a) any leaf is `verified_true` AND its parent chain confidence >= 0.7 → emit `RootCauseResult(found=True, chain=[...])`, or (b) all leaves are at depth 4 → emit `RootCauseResult(found=False, depth_capped=True)`.
5. Add `tests/test_hypothesis.py` with at least 4 cases: (a) seed creates a root with depth=0, (b) expand on depth=3 node returns children with depth=4 marked `status="depth_capped"` instead of further expansion, (c) verify-true short-circuits the walker, (d) all-inconclusive walk terminates with `found=False, depth_capped=True`.

**P — Plan:**

* `expand` calls claude-haiku-4-5 with a focused prompt: input is parent hypothesis, output is 1-3 child hypotheses with `evidence_query` filled in.
* Verifier dispatches on `evidence_query.kind`: `sql` → `sqlite3` query, `substring` → string match in fetched ingest event payload.
* Cost: every `expand` is a tracked LLM call against `token_usage`.

**E — Evidence:**

* `git diff` showing the four new files.
* `pytest tests/test_hypothesis.py -v` output: 4+ tests, all green.
* A worked example in the PR description showing a 3-level tree resolving to a verified root cause from a synthetic seed.

**V — Verification (machine-checkable):**

1. Files `src/alfred_doctor/hypothesis.py` and `src/alfred_doctor/verify.py` exist with the named classes and functions.
2. `Hypothesis.depth` is hard-capped at 4 — a parametrized test asserts that calling `expand` on a `depth=4` node returns `[]` and never invokes the LLM.
3. `pytest tests/test_hypothesis.py -v` exits 0 with at least 4 cases covering: seed-depth-0, depth-cap, verify-true short-circuit, all-inconclusive-terminates.
4. Tests assert that `verify` returns one of exactly three string literals: `"true"`, `"false"`, `"inconclusive"`.
5. Plan doc `plans/v1-ga/AD-f.md` exists with an APE/V Acceptance section byte-identical to this Linear description's.
