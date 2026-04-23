# E. soul-svc Gap Closure — v1.0 GA

*Epic owner: TBD · Mission Control v1.0 GA · Drafted 2026-04-23 (Plan sub E)*
*Amended 2026-04-23: DB-portability track added (S-09..S-12); parallel to S-01..S-08*
*Linear: SAL-SS-01..12 · Target repo: `salucallc/soul-svc` (prod variant only)*

## 1. Epic Summary

soul-svc is Saluca's canonical memory graph service. The v2.0.0 refactor (2026-04-13) left behind: two confirmed data-plane bugs (bulk-import TKHR indexing, duplicate-hash 409 mapping), one half-landed gap-close (a new `/v1/cot/capture` file-shim router that does NOT yet feed the `_memories` table), an unfinished PQ-backend wire-up, and a missing `/metrics` endpoint that Ops needs for Prometheus. **Every other "missing v2 endpoint" in the seed gap list is already live on main** (`session_lifecycle.py` exposes `/v1/session/close`, `/v1/session/capture`, `/v1/session/capture-jsonl`, `/v1/session/cot/flush`). This epic locks backward compatibility, burns down real bugs, finishes PQ backend wire-up behind feature flag, and closes observability + schema-migration documentation gaps so soul-svc is GA-grade.

**~3 days wall-time with parallel mesh execution. ~$0.75-$1.00 total cost.**

## 2. Repo Disambiguation

Confirmed against memory `reference_soul_svc_repos.md` and filesystem:

| Purpose | Path | GitHub | Use for |
|---|---|---|---|
| **Prod variant** | `C:/Users/cris/Desktop/soul-svc/` | `salucallc/soul-svc` | ALL tickets in this epic |
| **Paper variant** | `Z:/soul-svc/` | (private, paper PoC) | Out of scope |

## 3. Gap Inventory (Verified Against Current Code)

### Confirmed OPEN (tickets required)

1. **Bug A — `/v1/memory/import` skips TKHR topic indexing.** `routers/memory.py` lines 424-480: `import_memories` builds records via `_make_record()` and upserts, but unlike `write_memory` (lines 354-362) never calls `tkhr_mod.index_memory(memory_id, req.topics, ...)`. Records land in `_memories` with correct `session_id` (line 472), so `GET /v1/memory/{session_id}` DOES return them — user-observed "fail to link to sessions" was actually **topic-search invisibility** because TKHR was never populated. Memory is partly stale; canonical symptom still broken.

2. **Bug B — Duplicate-hash writes return 500, not 409.** `routers/memory.py` lines 337-341 and 475-479. Both `write_memory` and `import_memories` wrap `.upsert(...).execute()` in `try/except Exception` and re-raise as 500. No PostgreSQL `23505` / `UniqueViolation` handler. Only `409` in file is line 558 for "Upload already finalized".

3. **CoT shim half-landed.** PR#12 merged 2026-04-22 (SAL-2549) added `routers/cot_capture.py` but only writes to `/var/lib/soul-svc/cot/<session>.cot` JSONL on disk; does NOT insert into `_memories` or `_memory_topic_index`. Explicitly a "compat shim" per PR. The v2-native `/v1/session/cot/flush` (in-memory buffer) and `/v1/session/capture` (writes to `_memories`) ARE live, so question: delete disk shim, or give it TTL-based flush-to-DB? Needs decision.

4. **`/metrics` endpoint missing.** Grep confirms zero hits for `prometheus` or `/metrics` across repo. Ops epic's Prometheus scraper can't target soul-svc.

5. **SAL-2548 — PQ backend wire-up.** `pq_crypto_lib/` IS vendored (commit `c63003a`), `routers/challenge.py` IS live, migrations 013-017 applied. What's NOT done: Portal's Asphodel PQ Posture UI expects `POST /v1/challenge/verify` returning hybrid-signed tenant-pubkey receipt. Currently `challenge.py` mints nonces but **read-back / receipt-viewing surface isn't wired to UI**. Flag for Cristian scope.

6. **Schema migration story undocumented.** No Alembic, no `migrations_runner.py`. `migrations/*.sql` files exist but applied manually via Supabase SQL editor today (per `reference_supabase_ddl_access.md`). For GA: idempotent in-repo runner OR explicit runbook.

7. **MCP-tool endpoint audit.** `@salucallc/soul-mcp` npm package (v0.1.1) still hits production. Global CLAUDE.md says tools return 404 — but `session_lifecycle.py` DOES register those paths. **Hypothesis: 404 is MCP package using outdated path names, NOT server.** Ticket: smoke-test each MCP tool against deployed v2.

### 3.8 — DB-portability gap (added 2026-04-23)

8. **soul-svc is Supabase-SDK-coupled; appliance Postgres wiring is a placeholder.** The v1.0 GA appliance compose file `deploy/appliance/docker-compose.yml` declares `DATABASE_URL=postgresql://appliance@postgres:5432/soul` for the soul-svc container — but soul-svc's `routers/deps.py` reads `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` and calls `create_client(...)`. **Today the appliance cannot actually run soul-svc against its own bundled Postgres; every appliance customer would also need to provision Supabase**, which directly violates the sovereign-on-prem guarantee that is the appliance's entire value proposition.

   **Surface area:** `routers/deps.py` (auth + Supabase client factory), `routers/memory.py` (664 lines, ~20 `.execute()` sites), `routers/session_lifecycle.py` (574 lines), `routers/session.py` (615 lines), `routers/challenge.py`, `routers/admin.py`, plus ~15 other routers (topics, search, crypto, relay, vault, consent, a2a_audit, global_state, onboard, nexus, inference, account, billing, mesh, overview) that are NOT appliance-critical in v1.0 but will need the same sweep in a follow-up epic.

   **Design lock (no escalation):** asyncpg + thin repository layer. NOT SQLAlchemy-async. soul-svc SQL is direct (upsert/select/update/equality filters) with no complex joins or ORM relationships; asyncpg gives us native async, one-line pool init against any Postgres-wire backend, and zero ORM overhead. `asyncpg.create_pool(DATABASE_URL)` works against local Postgres, Supabase (direct Postgres, not the rpc surface), Neon, RDS, Cloud SQL, Timescale, and any future Postgres-compatible backend. SQLAlchemy adds session/metadata overhead without benefit since no ORM relationships need to be modeled.

   **Out-of-scope for this amendment:** the remaining ~15 non-appliance-critical routers (search, crypto, relay, vault, consent, etc.). Those get swept in a separate follow-up epic after v1.0 GA. If the appliance ships and a customer exercises those routes, fall-through to Supabase continues to work (SDK stays installed; only appliance-critical routes are refactored in S-09..S-12).

### Confirmed RESOLVED (no tickets needed)

- `/v1/session/close`, `/v1/session/capture-jsonl`, `/v1/session/cot/flush` — ALL live via `register_lifecycle_routes(session.router)` in `serve.py` lines 147-150. **Mark session memory stale.**
- `/v1/keys/mint` — doesn't exist under that path, but `/v1/admin/keys` (POST) does (`routers/admin.py` line 80). Tenants mint via admin. README naming mismatch is docs bug, not code bug.
- Harness registry auto-announce (SAL-2553), failover healthz chain (SAL-2555), Sparse-K admission (SAL-2554), MCP plug-in loader (SAL-2552) — all merged.

## 4. Decisions

### Locked (execute without escalation)

- Target: `salucallc/soul-svc` prod variant only
- Backward-compatible API changes only: bug fixes must not alter success-path response shapes
- Python 3.12, FastAPI, Supabase SDK ≥2.28 (retained for non-appliance-critical routers until follow-up epic)
- Model: `deepseek-v3.2:cloud` for Python bug fixes; `qwen3-coder:480b-cloud` for larger routers
- Every ticket ships pytest regression test + APE/V acceptance script passing on clean container
- **DB-portability (2026-04-23 amendment):** asyncpg + thin repository layer is locked as the backend-abstraction primitive. SQLAlchemy-async explicitly rejected (overhead without benefit; no ORM relationships to model). `DATABASE_URL` is the single configuration input; soul-svc refuses to start without it. Supabase becomes ONE supported backend, not THE backend.

### Open (needs Cristian's call)

- **D1 — CoT shim policy.** Keep `/v1/cot/capture` file shim alongside `/v1/session/cot/flush`, or merge into single v2-native route writing to `_memories` with `modality="cot"`? **Recommend: merge;** file shim redundant. Impacts S-03 scope.
- **D2 — PQ backend scope.** Finish hybrid-sig receipt endpoint for Portal UI (~4-6h), or defer until Karolin client lands on sovereign auth? Crypto lib vendored (Apache-2.0, `quantcrypt`), but exposing new verify path in prod requires **Cristian sign-off on wire format**. **Recommend: defer** to post-GA unless Portal wizard screen 4 blocks.
- **D3 — Observability ownership.** Ops owns Prometheus scraper config while soul-svc ships `/metrics`, or Ops owns both? **Recommend: soul-svc owns `/metrics`;** Ops owns scrape config. S-04 assumes this split.
- **D4 — Migration runner vs runbook.** Build `scripts/apply_migrations.py` connecting via `SUPABASE_SERVICE_KEY` running `exec_sql` per file (state in `_soul_migration_log`), or write runbook and accept manual paste? **Recommend: build runner** — ~~service-role key allows `rpc('exec_sql')` on Supabase projects where function exists~~ **AMENDED 2026-04-23:** runner uses asyncpg against `DATABASE_URL` (NOT Supabase rpc), per S-11. SS-06 now scope-downs to runbook + CLI docs; implementation lands via S-11. D4 answer is unchanged in spirit: YES build runner, but built on portable primitive.

## 5. Ticket Breakdown (12 tickets: 7 canonical gap-close + 1 conditional + 4 DB-portability track)

### 5.1 Canonical gap-closure (S-01..S-08) — critical path

| # | Title | APE/V | Effort | Deps | Model |
|---|---|---|---|---|---|
| S-01 | fix(memory): index TKHR topics on /v1/memory/import path | `pytest test_bulk_import_topics_queryable` — POST 3 memories with topics, GET /v1/topics/alpha returns all 3 memory_ids | S (2-3h) | none | deepseek-v3.2:cloud |
| S-02 | fix(memory): return 409 Conflict on duplicate content_hash | `pytest test_memory_dup_409` — first call 200, second 409 with `{existing_memory_id, content_hash}`; import batch with dup reports `skipped=1` not 500 | S (3-4h) | none (parallel S-01) | deepseek-v3.2:cloud |
| S-03 | refactor(cot): unify /v1/cot/capture into /v1/session native path | (a) /v1/cot/capture accepts current shape (deprecation-compat); now writes to `_memories` modality=cot; (b) file-shim path removed; (c) pytest asserts row in _memories queryable; (d) /var/lib/soul-svc/cot/ never created. **NOTE:** If S-10 lands first, build CoT router on asyncpg from day one. | M (6-8h) | **D1** decision; should land after S-10 if db-track is on track | qwen3-coder:480b-cloud |
| S-04 | feat(obs): add /metrics endpoint with request + DB latency counters | `curl /metrics` returns text/plain; includes `soul_http_requests_total`, `soul_http_request_duration_seconds` (histogram), `soul_db_query_duration_seconds{table}`, `soul_memory_writes_total{knowledge_tier}`; pytest scrape after 3 writes confirms increment | M (6-10h) | none (Ops epic owns scraper) | qwen3-coder:480b-cloud |
| S-05 | test(mcp-compat): smoke every mcp__alfred__soul_* tool against v2 main | New `test_mcp_v2_compat.py` invokes 8 endpoints with payloads matching `@salucallc/soul-mcp` v0.1.1 request shapes; all return 2xx. If mismatch, open bug subticket against npm repo | S (3-4h) | S-01, S-02 | deepseek-v3.2:cloud |
| S-06 | feat(infra): add scripts/apply_migrations.py with _soul_migration_log | **AMENDED 2026-04-23:** scope-down to runbook + CLI docs for the migration runner; actual implementation lands via **S-11** (asyncpg-based, portable). S-06 delivers: (a) docs/migrations.md — when to run, how to review output, recovery from partial apply; (b) CLI `--help` text; (c) PR template checklist item "did you run --apply after migration PR merged?". | S (3-4h, down from M) | **S-11** must merge first | qwen3-coder:480b-cloud |
| S-07 | docs(readme): update v2 endpoint surface + /v1/admin/keys clarification | (a) README lists every router in serve.py; (b) /v1/admin/keys documented as tenant-key-minting path; (c) ARCH.md diagram updated; (d) PRODUCTION_GAP.md deleted or replaced; CI job `docs-lint` verifies every `@router.post/get` decorator referenced in README. **AMENDED 2026-04-23:** README must document `DATABASE_URL` as the canonical backend config; Supabase-specific instructions moved to a separate "Supabase backend tips" section. | S (4-5h, +1h for db docs) | S-01..S-04, S-09..S-12 | qwen3-coder:480b-cloud |
| S-08 **(conditional)** | feat(pq): /v1/challenge/verify returns hybrid-signed tenant receipt | (a) POST with `{tenant_id, ed25519_sig, mldsa44_sig, nonce}` returns JWS-signed `{receipt, pubkey_sha, signed_at}`; (b) Portal wizard screen 4 can render "PQ-verified" badge; (c) pytest uses pq_crypto_lib.generate_keypair() to mint test key | **L (14-18h)** | **D2** approved + Cristian crypto review | qwen3-coder:480b-cloud |

### 5.2 DB-portability track (S-09..S-12) — parallel to S-01..S-08, does NOT block critical path

These tickets run in a parallel swimlane. S-09 starts Wave 0 alongside S-01/S-02/S-04/S-06. The track exists because soul-svc today is Supabase-SDK-coupled, which means the v1.0 GA appliance cannot actually stand up soul-svc against its bundled Postgres — every appliance customer would also have to provision Supabase, violating the sovereign-on-prem guarantee.

**Design lock:** asyncpg + thin repository layer. See §3.8 for rationale.

| # | Linear | Title | APE/V | Effort | Deps | Model |
|---|---|---|---|---|---|---|
| S-09 | SAL-2670 | refactor(db): introduce asyncpg repository layer, swap Supabase SDK in routers/memory.py | (a) `pytest test_asyncpg_pool_init` against local postgres container; (b) POST `/v1/memory/write` + `/v1/memory/import` green with asyncpg; (c) static `ast` assertion no `supabase` import in `routers/memory.py`; (d) startup refuses without `DATABASE_URL`; (e) `grep -c 'client.table' routers/memory.py == 0` | **L (10-14h)** | none (parallel to S-01..S-08) | qwen3-coder:480b-cloud |
| S-10 | SAL-2671 | refactor(db): swap Supabase SDK in session/challenge/admin/deps routers to asyncpg | (a) `test_auth_hybrid.py` + `test_session_continuity.py` unchanged; (b) new tests for session_close, challenge, admin-key mint via asyncpg; (c) bearer auth latency benchmark <=10% regression; (d) `grep -c 'client.table' routers/{session_lifecycle,session,challenge,admin,deps}.py == 0` | M (8-10h) | S-09 | qwen3-coder:480b-cloud |
| S-11 | SAL-2672 | refactor(infra): amend apply_migrations.py to use asyncpg (not rpc exec_sql) | (a) `--dry-run` default; (b) idempotent with `_soul_migration_log`; (c) SHA-drift detection; (d) bootstrap log table on fresh DB; (e) optional Neon-branch test; (f) `grep -c 'supabase\|rpc\|SUPABASE_SERVICE_KEY' scripts/apply_migrations.py == 0` | S (4-6h) | S-09 | deepseek-v3.2:cloud |
| S-12 | SAL-2673 | test(e2e): soul-svc smoke passes against 3 DATABASE_URL backends | (a) `tests/e2e/smoke_backends.sh --backend=local\|supabase\|neon` all exit 0; (b) 5 smoke assertions green each backend (`/v1/memory/write`, `/v1/memory/import`, `/v1/memory/{sid}`, `/v1/session/close`, `/metrics`); (c) CI workflow `.github/workflows/soul-svc-backend-portability.yml` runs local-backend on every PR; (d) no SUPABASE_SERVICE_KEY in harness | M (6-8h) | S-09, S-10, S-11 | qwen3-coder:480b-cloud |

## 6. Dependency Graph + Parallelization

```
Critical path (gap-close):
S-01 ──┐
S-02 ──┼─► S-05 ──► S-07
S-03 ──┘               ▲
S-04 ──────────────────┤
S-08 ───── (conditional; parallel to all above if approved)

DB-portability parallel track (NEW 2026-04-23):
S-09 ──┬─► S-10 ──┐
       │          ├─► S-12
       └─► S-11 ──┤
              │
              └──► S-06 (amended: runbook/docs only; was implementation)
                       ▲
                       └─── S-11 supersedes original S-06 impl
```

**Wave 0 (parallel, no blockers):** S-01, S-02, S-04, S-09 (DB-track starter) — ~2 days
  - Note: S-03 depended on D1 decision; now additionally waits on S-10 so CoT router builds on asyncpg from day one (avoids re-refactor)
**Wave 1 (parallel):** S-03, S-10, S-11 — ~1.5 days
**Wave 2:** S-05 (after S-01+S-02), S-06 (after S-11; now docs-only), S-12 (after S-09+S-10+S-11) — ~0.5-1 day
**Wave 3:** S-07 (docs after Wave 2) — ~0.5 day
**Side track:** S-08 if approved, independent

**Minimum critical path for GA: ~3-4 days with parallel mesh execution.** The DB track adds ~1 day to total wall-clock *only if it is on the critical path*; because it runs in parallel with S-01..S-08 and the longest chain is S-09 → S-10 → S-12 (~3 days), it lands inside the existing critical-path window.

**Is DB-track on the v1.0 GA critical path?** YES. Without S-09 the appliance docker-compose cannot stand up soul-svc against bundled Postgres. Without S-12 we haven't proven backend portability against real non-local targets. The release gate for v1.0.0-rc.1 now includes S-12 going green in CI.

## 7. Risk Register

| # | Risk | P | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Changing memory response shapes breaks alfred-coo-svc, portal, picoclaw-nano | Med | High | S-01 and S-02 add fields only, never rename/remove. S-05 test gate |
| R2 | 409 change mishandled by retry logic assuming 500=transient | Med | Med | Grep downstream repos (picoclaw-nano/providers/memory, portal handlers) for 500-retry; document 409 in release notes; preserve `detail` string format |
| R3 | `/metrics` scrape leaks tenant-id cardinality (label explosion + privacy) | **High** | Med | S-04 MUST NOT label with `tenant_id`. Use `knowledge_tier`, `modality`, `status_code` only. PR review gate |
| R4 | PQ receipt ships with wire format Cristian later changes | Med (if S-08) | High | D2 decision gate; security review + sign-off on JWS claims schema before merge |
| R5 | Migration runner auto-applies bad migration to prod Supabase | Low | Critical | `--dry-run` default; explicit `--apply` required; `_soul_migration_log` SHA check detects partial; refuses to run if `SUPABASE_SERVICE_KEY` unset |

## 8. Cost Estimate

| Ticket | Input | Output | Model | Est $ |
|---|---|---|---|---|
| S-01 | 20k | 6k | deepseek-v3.2 | ~$0.02 |
| S-02 | 20k | 6k | deepseek-v3.2 | ~$0.02 |
| S-03 | 40k | 12k | qwen3-coder-480b | ~$0.05 |
| S-04 | 35k | 10k | qwen3-coder-480b | ~$0.04 |
| S-05 | 25k | 8k | deepseek-v3.2 | ~$0.02 |
| S-06 (scope-downed) | 20k | 6k | qwen3-coder-480b | ~$0.03 |
| S-07 | 30k | 10k | qwen3-coder-480b | ~$0.04 |
| S-08 (cond) | 60k | 20k | qwen3-coder-480b | ~$0.10 |
| **Gap-close subtotal w/o S-08** | | | | **~$0.22** |
| S-09 (db-track) | 60k | 18k | qwen3-coder-480b | ~$0.09 |
| S-10 (db-track) | 55k | 15k | qwen3-coder-480b | ~$0.08 |
| S-11 (db-track) | 25k | 8k | deepseek-v3.2 | ~$0.03 |
| S-12 (db-track) | 40k | 12k | qwen3-coder-480b | ~$0.05 |
| **DB-track subtotal** | | | | **~$0.25** |
| **Total w/o S-08** | | | | **~$0.47** |
| **Total w/ S-08** | | | | **~$0.57** |

With Hawkman QA retries (2-3× overhead): **$1.00 – $1.50 realistic budget** (was $0.75 – $1.00; added ~$0.25-0.50 for DB track).

## 9. Cross-Epic Touchpoints

### Exposes
- `/metrics` → Ops epic (Prometheus scraper). S-04 deliverable.
- `_soul_migration_log` table → Ops epic (if fleet replication syncs schema state)
- `/v1/memory/write` content_hash + signature → Aletheia (memory-write verification distinguishes 409-idempotent from 500-error)
- `/v1/memory/import` post-fix → Fleet (bulk replication for cross-node sync)
- `/v1/challenge/verify` (if S-08) → Portal Asphodel PQ UI

### Consumes
- Ops: scrape config for `/metrics`
- Aletheia: which assertions Aletheia wants verifiable (guides metrics/labels in S-04)
- Fleet: replication API contract (if Fleet needs new `/v1/replica/sync`, new ticket — not this epic)
- Portal: JWS claim schema (if S-08 approved)
