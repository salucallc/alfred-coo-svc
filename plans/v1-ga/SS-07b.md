# SS-07B: soul-svc ARCH.md diagram + PRODUCTION_GAP.md cleanup + docs-lint coverage assertion

**Parent:** SS-07 (soul-svc docs hardening) — see `plans/v1-ga/SS-07.md`
**Linear:** SAL-3073
**Wave:** 3

## Context

Closes the remaining APE/V criteria of SS-07: (c) [ARCH.md](<http://ARCH.md>) diagram updated; (d) PRODUCTION_GAP.md deleted or replaced; (e) docs-lint CI verifies every `@router` decorator referenced. Sibling SS-07A handled README + /v1/admin/keys.

## APE/V Acceptance

**A â€” Action:**

1. Update `ARCH.md`:
   * Refresh the architecture diagram (mermaid block) to show every current router module + its route group.
   * Reflect v2.0.0 surface (memory write/import/upload-md, overview digest/chat, session init).
   * Remove any references to dead routes (`/v1/session/close`, `/v1/session/capture-jsonl`, `/v1/session/cot/flush`).
2. Either delete `PRODUCTION_GAP.md` or replace its contents with a tombstone pointing to the current Linear MC v1 GA project; do whichever the existing repo conventions favor (inspect git log on the file).
3. Extend the docs-lint CI workflow added in `soul-svc#48` to:
   * Parse all `@router.<method>("...")` and `@app.<method>("...")` decorators from Python sources.
   * Fail the job if any route path is absent from `README.md`.
   * Surface the missing routes in the failure log.
4. Add a `tests/test_docs_router_coverage.py` pytest invocation of the same coverage check (so it runs both as CI workflow and as a unit test).

**P â€” Plan:**

* Use `ast` module in the docs-lint helper to extract decorator string args (path arg of `@router.method`).
* Parse README headings/tables for path occurrences via regex.
* Compute set difference; failure if non-empty.

**E â€” Evidence:**

* `git diff` of [ARCH.md](<http://ARCH.md>), PRODUCTION_GAP.md change, workflow patch, new test.
* Workflow run on the PR: green.
* Negative test: temporarily comment out a route's README row, workflow fails with a clear "router path not in README: <path>" message; revert before merge.

**V â€” Verification (machine-checkable):**

1. File `ARCH.md` exists and its mermaid block contains at least one node per router module.
2. `grep -E "/v1/(session/close|session/capture-jsonl|session/cot/flush)" ARCH.md` returns 0 (dead routes purged).
3. PRODUCTION_GAP.md is either absent (`! -e`) or its body is small (under 200 bytes) and contains the word `tombstone` or `superseded`.
4. File `tests/test_docs_router_coverage.py` exists.
5. `pytest tests/test_docs_router_coverage.py` exits 0.
6. The docs-lint workflow YAML invokes the same coverage check (`grep -q "docs_router_coverage" .github/workflows/docs-lint.yml`).
