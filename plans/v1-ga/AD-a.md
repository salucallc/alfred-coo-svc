# AD-a: Ingest service + SQLite timeseries schema

**Parent:** alfred-doctor epic — see `plans/v1-ga/AD.md`
**Linear:** [SAL-3281](https://linear.app/saluca/issue/SAL-3281)
**Wave:** wave-1

## Context

Step 1 of the architectural skeleton. Builds the ingest layer that pulls signals from five sources every 15 minutes and writes them into a SQLite timeseries DB at `/var/lib/alfred-doctor/state.db`. This is the foundation every later wave reads from. Idempotent on re-run within the same interval (uses (source, interval_id) as the natural key so re-firing the cron is safe).

## Target paths

* `src/alfred_doctor/ingest.py`
* `src/alfred_doctor/db_schema.sql`
* `tests/test_ingest.py`
* `plans/v1-ga/AD-a.md`

## Dependencies

None upstream. AD-b, AD-c, AD-d, AD-e, AD-f, AD-g, AD-h all depend on AD-a.

## APE/V Acceptance

**A — Action:**

1. Add `src/alfred_doctor/db_schema.sql` defining tables: `ingest_events(id, source, interval_id, captured_at, payload_json)`, `token_usage(id, loop, model, tokens_in, tokens_out, captured_at)`, `runbook(pattern_id, weight, last_outcome, updated_at)` (runbook is read-empty here; AD-g writes to it). Index `(source, interval_id)` UNIQUE.
2. Add `src/alfred_doctor/ingest.py` with class `Ingestor` exposing `Ingestor.run(interval_id: str) -> int` that polls all five sources (journal, mesh, github, linear, dashboard) and inserts rows. Uses `INSERT OR IGNORE` against the unique index, so re-running the same `interval_id` is a no-op.
3. Source adapters live as private methods: `_pull_journal`, `_pull_mesh`, `_pull_github`, `_pull_linear`, `_pull_dashboard`. Each returns a list of dict payloads. Adapters can be mocked in tests.
4. Add `tests/test_ingest.py` with at least 4 cases: one per source adapter (journal, mesh, github, linear) that mocks the source and asserts a row lands in `ingest_events` with the correct `source` value and parseable `payload_json`.

**P — Plan:**

* Use stdlib `sqlite3` for the DB, no ORM.
* Subprocess `journalctl -u alfred-coo --since "15 min ago" --output=json` for journal.
* HTTP GET against existing mesh + github + linear + dashboard endpoints; reuse env vars from `/c/saluca-deploy/.env` (LINEAR_API_KEY, GITHUB_TOKEN, etc.).
* Tests use pytest monkeypatch for the adapter source calls; the DB writes are real against a tmp SQLite path.

**E — Evidence:**

* `git diff` showing the four new files.
* `pytest tests/test_ingest.py -v` output: 4+ tests, all green.
* One real run against the live Oracle VM showing rows landing in `/var/lib/alfred-doctor/state.db` (captured in PR description).

**V — Verification (machine-checkable):**

1. File `src/alfred_doctor/ingest.py` exists and contains class `Ingestor` with method `run`.
2. File `src/alfred_doctor/db_schema.sql` exists and creates all three tables (`ingest_events`, `token_usage`, `runbook`) plus the UNIQUE index on `(source, interval_id)`.
3. `pytest tests/test_ingest.py -v` exits 0 with at least 4 test cases (one per adapter: journal, mesh, github, linear).
4. Re-running `Ingestor.run(interval_id)` with the same `interval_id` against the same DB does not increase the row count (idempotency assertion in tests).
5. Plan doc `plans/v1-ga/AD-a.md` exists and contains an APE/V Acceptance section byte-identical to this Linear description's APE/V Acceptance section (Hawkman gate-1 byte-compare).
