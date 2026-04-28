# AD-h: Dashboard integration (v8-doctor route)

**Parent:** alfred-doctor epic — see `plans/v1-ga/AD.md`
**Linear:** [SAL-3288](https://linear.app/saluca/issue/SAL-3288)
**Wave:** wave-3

## Context

Step 8 of the architectural skeleton. Extend the existing v8-pipeline dashboard at `http://100.105.27.63:8085/v8-pipeline` with two pieces:

1. A header strip showing the current alfred-doctor verdict (latest investigation summary) and a count of open hypotheses.
2. A new route `/v8-doctor/<finding-id>` that renders the full hypothesis tree for a given investigation, with each node showing `statement`, `confidence`, `status`, and `evidence_query`.

Routes are added to the existing dashboard server (Python/FastAPI per the v8-pipeline service); no new service is created.

## Target paths

* `src/alfred_doctor/dashboard_routes.py`
* `src/alfred_doctor/templates/v8_doctor.html`
* `src/alfred_doctor/templates/v8_pipeline_header.html` (a partial included into the existing v8-pipeline template)
* `tests/test_dashboard_routes.py`
* `plans/v1-ga/AD-h.md`

## Dependencies

Upstream: AD-a (reads `state.db`), AD-d (reads `InvestigationResult`), AD-f (reads `HypothesisTree`).
Downstream: none (terminal child).

## APE/V Acceptance

**A — Action:**

1. Add `src/alfred_doctor/dashboard_routes.py` with FastAPI `APIRouter` exposing two endpoints:
   * `GET /v8-doctor/{finding_id}` → renders `v8_doctor.html` with the hypothesis tree for that finding.
   * `GET /v8-doctor/_header` → returns the partial `v8_pipeline_header.html` rendered with the latest verdict + open-hypothesis count.
2. Add `src/alfred_doctor/templates/v8_doctor.html` rendering the tree as a nested `<ul>` of nodes, each showing `statement`, `confidence` (as percent), `status` (with a colored badge: green for verified_true, red for verified_false, gray for open, amber for depth_capped), and `evidence_query`.
3. Add `src/alfred_doctor/templates/v8_pipeline_header.html` as a Jinja partial showing the verdict line + open-hypothesis count, intended to be included at the top of the existing `v8_pipeline.html` template.
4. Wire the router into the existing v8-pipeline FastAPI app via a single `app.include_router(...)` call (the diff to the existing app file is part of this PR; do NOT touch other routes).
5. Add `tests/test_dashboard_routes.py` with FastAPI `TestClient` cases: (a) GET `/v8-doctor/{finding_id}` returns 200 and HTML containing the seed surprise text, (b) `/v8-doctor/{nonexistent}` returns 404, (c) GET `/v8-doctor/_header` returns 200 and HTML containing the latest verdict text.

**P — Plan:**

* Use the existing `Jinja2Templates` instance from the v8-pipeline app; do not register a second one.
* Read tree nodes from `state.db.hypotheses` (the table created when AD-f runs; see AD-f schema).
* Test fixtures pre-seed `state.db` with a synthetic investigation + 3-node tree; the live dashboard service path itself is not exercised by tests.

**E — Evidence:**

* `git diff` showing the four new files plus the one-line `include_router` insertion in the existing app file.
* `pytest tests/test_dashboard_routes.py -v` output: 3+ tests, all green.
* One real screenshot of `/v8-doctor/<finding-id>` rendered against a seeded tree, pasted into the PR description.

**V — Verification (machine-checkable):**

1. File `src/alfred_doctor/dashboard_routes.py` exists with an `APIRouter` exposing exactly two GET endpoints under `/v8-doctor/...`.
2. Both template files exist and reference the expected variable names (`tree`, `verdict`, `open_count`).
3. `pytest tests/test_dashboard_routes.py -v` exits 0 with at least 3 cases covering: 200 happy path, 404 not-found, header partial 200.
4. Tests assert that the rendered HTML for `/v8-doctor/{finding_id}` contains the literal text of `tree.root.statement`.
5. Plan doc `plans/v1-ga/AD-h.md` exists with an APE/V Acceptance section byte-identical to this Linear description's.
