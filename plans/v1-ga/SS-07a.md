# SS-07A: soul-svc README router enumeration + /v1/admin/keys section

**Parent:** SS-07 (soul-svc docs hardening) — see `plans/v1-ga/SS-07.md`
**Linear:** SAL-3072
**Wave:** 3

## Context

SS-07's APE/V calls for: (a) README lists every router in `serve.py`; (b) `/v1/admin/keys` documented as tenant-key-minting path; (c) [ARCH.md](<http://ARCH.md>) diagram updated; (d) PRODUCTION_GAP.md deleted or replaced; (e) docs-lint CI verifies every `@router` decorator referenced.

## APE/V Acceptance

**A â€” Action:**

1. Read `serve.py` (and any sibling router-mount files); enumerate every `app.include_router(...)` and every `@router.<method>("...")` decorator under `routers/` (or the equivalent directory).
2. Update `README.md` with an "Endpoint Surface" section that:
   * Lists every router module by name.
   * Lists every route (method + path) under each router.
   * Marks v2.0.0-current routes vs deprecated routes.
3. Add a dedicated subsection `### /v1/admin/keys â€” Tenant Key Minting` describing: purpose, auth requirement (admin bearer), request body shape, response shape, example `curl`.
4. Cross-link the admin-keys section from the top-level README table of contents.

**P â€” Plan:**

* `grep -nE "(include_router|@router\\.|@app\\.)" serve.py routers/*.py` to enumerate.
* Author the section as a markdown table (method | path | description | router-module).
* For `/v1/admin/keys`, mirror the request/response shapes from the route handler's pydantic models.

**E â€” Evidence:**

* `git diff README.md` showing the new Endpoint Surface section.
* Output of the grep enumeration captured in PR description for review.

**V â€” Verification (machine-checkable):**

1. `grep -c "^| " README.md` (after edit) returns at least the count of routes in the codebase (table rows).
2. `grep -c "/v1/admin/keys" README.md` returns at least 2 (mention + dedicated section).
3. `grep -E "^### /v1/admin/keys" README.md` matches the dedicated subsection header.
4. `grep -cE "(@router\\.|@app\\.)(get|post|put|delete|patch)" routers/*.py serve.py` count matches the number of route rows in the README table.
