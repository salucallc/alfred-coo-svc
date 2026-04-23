# C. Fleet Mode + Endpoint Persona

*Epic owner: TBD · Mission Control v1.0 GA · Drafted 2026-04-23 (Plan sub C)*
*Linear: SAL-F01..25 + SAL-C-26..29 (multi-tenant amendment 2026-04-23) · Kickoff: 2026-04-23*

*Amendment 2026-04-23 (multi-tenant flip): C6 flipped from **defer → ship in GA**. Rationale: (1) multi-tenant IS the MSSP story; (2) Tiresias MSSP patterns exist in salucallc/tiresias-core with tiresias-partner cluster live on GKE and first MSSP onboarded 2026-04-07; (3) retrofitting multi-tenant to a single-tenant system costs 2-3× more than day-one design; (4) coherent with PQ design since E2 is also locked to ship in GA — multi-tenant + PQ together = one design: per-tenant keys, hybrid-sig per tenant, receipt binds tenant identity. Four tickets (C-26..C-29) added at end of this plan; Epic-C LoE bumps ~30-35h within existing parallel slots (no wall-clock delay).*

## 1. Epic Summary

Transform Mission Control from single-host hub into hub-of-spokes fleet by shipping **endpoint persona** — a field-deployable footprint of `alfred-coo-svc` that registers with a central hub, operates semi-autonomously with local context, and syncs back over **outbound-initiated WSS with HTTP long-poll fallback**. The endpoint daemon is the **same binary** as `alfred-coo-svc`; persona is selected at boot via `COO_MODE=hub|endpoint` plus a mounted `persona.yaml`. Delivers: (a) registration + credential-exchange protocol, (b) memory replication with explicit conflict resolution, (c) degraded-mode contract, (d) policy/persona push mechanism, (e) network-topology layer. UI for hub-side fleet management is out of scope — CLI/API enough for GA.

**Success:** one hub, three endpoints on different NATed networks, all executing assigned mesh tasks, surviving 10-minute hub blackout, reconciling memory diffs within 60s of recovery.

**25 tickets, 6 waves, ~3 weeks with 2-3 concurrent workers, ~$1-2 model $ (Ollama Max covers bulk).**

## 2. Architecture

### 2.1 Topology

```
                             Internet / Tailscale
                                       |
                        +--------------+--------------+
                        |    HUB (on-prem appliance)  |
                        |    persona=hub              |
                        |  +-----------------------+  |
                        |  | caddy  :443 TLS       |  |
                        |  +-----+---------+-------+  |
                        |        |         |          |
                        |  +-----v---+  +--v--------+ |
                        |  | fleet-  |  | soul-svc  | |
                        |  | gateway |<-| v2.1 (mesh| |
                        |  | :8090   |  | +fleet)   | |
                        |  | WS/SSE  |  | :8080     | |
                        |  +----+----+  +-----+-----+ |
                        |       ^             ^       |
                        |       |       +-----+-----+ |
                        |       |       | alfred-coo| |
                        |       |       | COO_MODE= | |
                        |       |       | hub       | |
                        |       |       +-----------+ |
                        +-------+---------------------+
                                |
               outbound only (WSS/443 — endpoint initiates)
                                |
        +-----------------------+-----------------------+
        |                       |                       |
 +------v-------+       +-------v------+        +-------v------+
 | ENDPOINT A   |       | ENDPOINT B   |        | ENDPOINT C   |
 | customer NAT |       | customer NAT |        | customer NAT |
 | COO_MODE=    |       | COO_MODE=    |        | COO_MODE=    |
 | endpoint     |       | endpoint     |        | endpoint     |
 | alfred-coo   |       | alfred-coo   |        | alfred-coo   |
 | soul-lite    |       | soul-lite    |        | soul-lite    |
 | (sqlite+WAL) |       | (sqlite+WAL) |        | (sqlite+WAL) |
 | local MCPs   |       | local MCPs   |        | local MCPs   |
 +--------------+       +--------------+        +--------------+
```

### 2.2 Protocol stack

| Layer | Hub | Endpoint |
|---|---|---|
| Transport | TLS 1.3 at caddy, mTLS optional beyond GA | TLS 1.3 outbound; no inbound ports |
| Session | Persistent WS `/v1/fleet/link`; fallback HTTP long-poll `/v1/fleet/poll` | Exponential backoff (1s→60s cap, ±20% jitter) |
| Framing | JSON envelope `{v:1, type, msg_id, corr_id?, payload}` | Same |
| Auth | Bootstrap: registration token (one-shot, 15-min TTL). Steady-state: endpoint API key (`sk_endpoint_<orgslug>_<endpoint_id>_<sha256>`) rotated every 24h via heartbeat piggyback | Same |
| Identity | Hub issues `endpoint_id` (UUID v4) + signed persona bundle | Endpoint stores in `/etc/alfred-endpoint/identity.json` (mode 0600) |
| Application | soul-svc fleet router (`/v1/fleet/*`) + existing mesh router | Endpoint persona + soul-lite |

### 2.3 Component deltas vs hub

**Hub additions:**
- `soul-svc`: new `routers/fleet.py` under `/v1/fleet/*`
- `soul-svc`: migration `0007_fleet_endpoints.sql` (4 tables)
- New `fleet-gateway` sidecar (port 8090) for WS fan-out + backpressure — keeps long-lived connections out of soul-svc's request workers
- `alfred-coo-svc`: `persona_loader.py` branches on `COO_MODE`
- CLI `mcctl`: `endpoint list|show|revoke|push-policy|tail`

**Endpoint footprint:**
- Compose: `alfred-coo-svc` (endpoint mode) + `soul-lite` (sqlite-backed, same `/v1/memory/*` API, scoped local tenant) + optional local MCP shims
- No postgres, no open-webui, no portal — headless

### 2.4 Network topology — LOCKED

**Endpoint-initiated persistent outbound WSS**, HTTP long-poll fallback. Rationale: customer networks universally permit outbound 443, block inbound; matches existing `mesh_heartbeat` pattern; simplifies NAT/DDNS/port-forward. Hub cannot "call" endpoint — hub→endpoint is "hub enqueues, endpoint drains on next WS read." Fallback to HTTP long-poll if a customer forbids outbound WebSocket.

## 3. Protocol Specs

### 3.1 Registration flow

Endpoint boots with one-time `registration_token` (admin pastes into `.env`) + hub URL.

**`POST /v1/fleet/register` request:**
```json
{
  "registration_token": "rt_01J9Z2...X7",
  "hw_fingerprint": {
    "machine_id": "b1c2...",
    "os": "linux", "kernel": "6.1.0-29-amd64",
    "cpu_count": 4, "mem_gb": 16,
    "docker_version": "25.0.3"
  },
  "binary": {"image": "salucallc/alfred-coo-svc", "tag": "v1.0.0-ga", "image_digest": "sha256:abcd..."},
  "requested_persona": "endpoint-default",
  "site": {"site_code": "cust-acme-sfo", "timezone": "America/Los_Angeles", "contact_email": "ops@acme.example"},
  "public_key_pem": "-----BEGIN PUBLIC KEY-----\n..."
}
```

**`201 Created` response:**
```json
{
  "endpoint_id": "ep_01J9Z2Q8K3M8F7B2V1N5Y4R6PZ",
  "api_key": "sk_endpoint_acme_01J9Z2Q8_3f4a...9c2e",
  "api_key_expires_at": "2026-04-24T12:00:00Z",
  "hub": {
    "fleet_ws_url": "wss://hub.example/v1/fleet/link",
    "fleet_poll_url": "https://hub.example/v1/fleet/poll",
    "soul_url": "https://hub.example"
  },
  "persona_bundle": {
    "persona_id": "endpoint-default", "version": "1.0.0",
    "hash": "sha256:...", "signature": "ed25519:...",
    "config": { "...see 3.4..." }
  },
  "memory_sync": {
    "mode": "hybrid",
    "push_interval_seconds": 30, "pull_interval_seconds": 60,
    "conflict_strategy": "hub_wins_with_journal"
  },
  "heartbeat_interval_seconds": 15,
  "hub_public_key_pem": "-----BEGIN PUBLIC KEY-----\n..."
}
```

### 3.2 Heartbeat (15s default)

WS frame (or POST `/v1/fleet/heartbeat` in poll mode):
```json
{
  "v": 1, "type": "fleet.heartbeat", "msg_id": "hb_01J9Z3...",
  "payload": {
    "endpoint_id": "ep_01J9Z2...", "ts": "2026-04-23T14:22:05.124Z",
    "uptime_s": 86432, "persona_version": "1.0.0",
    "mode_state": "normal|degraded|recovering",
    "load": {"cpu_pct": 12.4, "mem_pct": 34.1, "inflight_tasks": 2},
    "counters": {"tasks_completed_total": 412, "tasks_failed_total": 7, "memory_writes_local": 1893, "memory_writes_synced": 1890},
    "sync_cursor": {"memory_last_synced_seq": 1890, "policy_version": "1.0.0", "persona_version": "1.0.0"},
    "degraded_since": null, "hw_fingerprint_hash": "sha256:..."
  }
}
```

Hub ack:
```json
{
  "v": 1, "type": "fleet.heartbeat.ack", "corr_id": "hb_01J9Z3...",
  "payload": {
    "hub_ts": "2026-04-23T14:22:05.201Z",
    "pending_commands": 3,
    "policy_latest_version": "1.0.1",
    "persona_latest_version": "1.0.0",
    "api_key_rotation": null
  }
}
```

### 3.3 Memory replication — hybrid pull+push with monotonic sequences

**Migration:**
```sql
CREATE TABLE fleet_memory_sync_log (
  global_seq      BIGSERIAL PRIMARY KEY,
  endpoint_id     TEXT NOT NULL REFERENCES fleet_endpoints(endpoint_id),
  local_seq       BIGINT NOT NULL,
  memory_id       UUID NOT NULL,
  content_hash    TEXT NOT NULL,
  op              TEXT NOT NULL,  -- 'upsert' | 'delete'
  applied_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(endpoint_id, local_seq)
);
```

**Push (endpoint → hub) 30s or queue ≥50:**
```json
POST /v1/fleet/memory/push
{
  "endpoint_id": "ep_01J9Z2...",
  "batch": [{
    "local_seq": 1891, "op": "upsert",
    "memory": {
      "memory_id": "mem_01J9Z...", "tenant_id": "...",
      "topics": ["local-incident", "acme-sfo"],
      "content": "...", "content_hash": "sha256:...",
      "created_at": "2026-04-23T14:21:00Z",
      "source": {"persona": "endpoint-default", "agent_id": "alfred-coo-endpoint-a"}
    }
  }]
}
```

Response: `{"accepted_up_to_local_seq": 1891, "rejected": [], "global_seq_range": [20431, 20431]}`

**Pull (endpoint ← hub) 60s:** `GET /v1/fleet/memory/pull?since_global_seq=20430&limit=200` → rows with `next_global_seq` cursor.

**Conflict resolution: `hub_wins_with_journal`.**
- Hub treats memory as append-only; `memory_id` NOT primary key on hub; `global_seq` is. "Conflict" collapses to ordering, not overwrite.
- Content_hash dedupe: identical hash → idempotent advance of `accepted_up_to_local_seq`.
- `*.singleton` topic with content_hash mismatch → hub writes `fleet.conflict` journal row; `mcctl endpoint conflicts` surfaces for human review.

### 3.4 Policy/persona push

Push on change; heartbeat ack carries `policy_latest_version`. Endpoint pulls if diff.

`GET /v1/fleet/policy?since_version=1.0.0`:
```json
{
  "policy_version": "1.0.1", "policy_hash": "sha256:...", "signature": "ed25519:...",
  "config": {
    "tool_allowlist": ["mcp.github.read", "mcp.linear.write", "local.fs.read"],
    "mesh_scope": {"allowed_personas": ["endpoint-default", "alfred-coo-endpoint-a"], "allowed_topics_prefix": ["acme-sfo."]},
    "model_routing": {
      "default": "deepseek-v3.2:cloud",
      "synthesis": "qwen3-coder:480b-cloud",
      "triage": "openrouter:hermes-3-llama-3.1-405b:free"
    },
    "degraded_mode": {
      "tolerance_seconds": 900,
      "tool_fallback": {"mcp.github.read": "cache_then_503", "mcp.linear.write": "queue_and_drain", "local.fs.read": "passthrough"}
    },
    "heartbeat_interval_seconds": 15
  }
}
```

**Application:** next-loop-boundary between task executions. In-flight tasks keep old policy. Emergency policies (`"apply": "immediate"`) interrupt + requeue.

### 3.5 Degraded-mode matrix

Degraded = WS disconnected >30s OR heartbeat ack missing for 3 consecutive intervals. Severe = degraded > `tolerance_seconds` (default 900).

| Capability | Normal | Degraded (< tolerance) | Severe (> tolerance) |
|---|---|---|---|
| Heartbeat | emit every 15s | buffer last 40 for replay | buffer 240, drop oldest |
| Memory write local | → sqlite + async push | → sqlite, queue locally | same; escalate to on-disk log if queue >10k |
| Memory read local | local sqlite | local sqlite | local sqlite |
| Memory read cross-endpoint | via pull | cached only + `X-Fleet-Stale: true` | cache only + `stale_since` |
| `mesh.task.claim` new | claim from hub | **NO new claims**; drain local only | NO; enter idle loop |
| `mesh.task.complete` | post to hub | queue `pending_complete.jsonl` | disk-backed queue |
| `mcp.github.*` | passthrough to hub | `cache_then_503` | 503 |
| `mcp.linear.write` | passthrough | `queue_and_drain` | queue |
| `local.fs.*` | passthrough | passthrough | passthrough |
| LLM inference | policy-routed | same (endpoint calls providers directly) | same |
| Policy pull | on ack signal | frozen at last-known | frozen |
| Key rotation | scheduled | skipped, extended grace | **quarantine** if key expires: no writes, read-only, refuses tasks |

**Recovery:** on WS reconnect, endpoint sends `fleet.reconcile` with cursor; hub responds with plan. Order: policy → persona → inbox → memory push → memory pull → resume. Must complete within 60s for <100k buffered writes.

## 4. Decisions

### Locked
1. Same binary, persona-differentiated (`COO_MODE=hub|endpoint`)
2. Endpoint-initiated persistent WSS, HTTP long-poll fallback
3. Append-only memory with monotonic seqs + hub global seq, `hub_wins_with_journal`
4. Registration token → scoped api_key rotated daily, ed25519-signed persona bundles
5. soul-lite for endpoint (sqlite + WAL)
6. No hub admin UI this epic — CLI + APIs only
7. `fleet-gateway` sidecar owns WS fan-out

### Open — Cristian's call
| # | Question | Recommendation |
|---|---|---|
| O1 | Endpoint cloud model spend: direct or proxy via hub? | **Direct** (lower latency, hub not bottleneck; duplicates API-key distribution). Locked-unless-overridden. |
| O2 | `fleet-gateway` new service or embed in soul-svc? | **Separate sidecar** — soul-svc workers shouldn't hold long-lived connections |
| O3 | Cross-endpoint memory reads: always via hub pull, or endpoint→endpoint direct? | **Hub-relayed only in v1.0 GA.** Direct → v1.1 |
| O4 | Persona bundle signing key rotation? | **Annual for GA.** Script but don't automate |
| O5 | Quarantine: wipe local queue or preserve? | **Preserve** (operator-triggered flush via `mcctl endpoint drain`) |
| O6 | ~~Multi-tenant endpoints (one endpoint, two customer orgs)?~~ | ~~No for GA. One endpoint = one site_code = one tenant scope~~ **FLIPPED 2026-04-23 → YES, ships in GA.** One endpoint can host N tenants. Tenant binding enforced at soul-lite repository boundary + per-tenant auth + per-tenant policy bundles. See C-26..C-29. |
| **O7** | **If customer forbids outbound WebSocket entirely, per-endpoint transport-selection flag?** | Add to policy bundle before kicking off F04 |

## 5. Ticket Breakdown (25 tickets)

| # | Title | APE/V | Size | Deps | Model |
|---|---|---|---|---|---|
| F01 | soul-svc migration 0007 for fleet tables | `\d fleet_endpoints` 8 cols; all 4 tables present; idempotent on reapply | S | — | qwen3-coder:480b-cloud |
| F02 | `/v1/fleet/register` endpoint | Valid token → 201 + endpoint_id/api_key; reused → 409 `token_used`; expired → 400 `token_expired`; pytest green | M | F01 | qwen3-coder:480b-cloud |
| F03 | `mcctl token create` CLI | `mcctl token create --site acme-sfo --ttl 15m` prints one-shot; DB row created; TTL enforced | S | F01 | deepseek-v3.2:cloud |
| F04 | `fleet-gateway` sidecar WS upgrade on `/v1/fleet/link` | `wscat` establishes; invalid key → 401 close; 100 concurrent clients 5min no leak | M | F02 | qwen3-coder:480b-cloud |
| F05 | Heartbeat protocol hub-side | ack p95 <500ms local; 3 missed → `mode_state=degraded`; visible in `/v1/fleet/endpoints/{id}` | M | F04 | qwen3-coder:480b-cloud |
| F06 | Heartbeat protocol endpoint-side | `COO_MODE=endpoint` posts every 15s; tcpdump shows frames; recovers from 30s drop without restart | M | F05 | qwen3-coder:480b-cloud |
| F07 | Introduce `COO_MODE` + `persona_loader.py` | `COO_MODE=hub` boots existing (regression green); `endpoint` disables hub-scope claim loop; `persona.yaml` read + sig-verified | M | (parallel F01) | qwen3-coder:480b-cloud |
| F08 | `soul-lite` service (sqlite + /v1/memory/*) | POST→local row; GET search works; WAL file in /data; 10k writes <5s | M | — | qwen3-coder:480b-cloud |
| F09 | Endpoint memory push loop | 1000 writes reflected in hub `fleet_memory_sync_log` within 60s; `local_seq` monotonic; dup push idempotent | M | F06, F08 | qwen3-coder:480b-cloud |
| F10 | Endpoint memory pull loop | Two endpoints A, B; A writes `shared.*`; B's search returns within 90s | M | F09 | qwen3-coder:480b-cloud |
| F11 | Memory conflict journal | Force content_hash conflict on `*.singleton`; hub writes `fleet.conflict` row; `mcctl endpoint conflicts` lists | S | F10 | deepseek-v3.2:cloud |
| F12 | `/v1/fleet/policy` + signer | Returns bundle with ed25519 sig; tampered → `openssl verify` fails; unit test green | M | F01 | qwen3-coder:480b-cloud |
| F13 | Endpoint applies policy on next boundary | Push new `tool_allowlist`; endpoint logs `policy.applied version=1.0.1` between tasks; in-flight task finishes under old policy | M | F12, F07 | qwen3-coder:480b-cloud |
| F14 | Emergency policy push (immediate) | `mcctl policy push --immediate` interrupts endpoint <5s; in-flight task requeued with `requeue_reason=policy_immediate` | S | F13 | deepseek-v3.2:cloud |
| F15 | API-key rotation on heartbeat ack | Test harness flips DB key; next ack carries rotation block; endpoint stores new, old 401s after 60s grace | M | F05 | qwen3-coder:480b-cloud |
| F16 | Degraded-mode tool behavior matrix | Cut hub connectivity; 8-case matrix passes (github cached→503; linear enqueues; fs passthrough; reconnect drains) | **L** | F07, F13 | qwen3-coder:480b-cloud |
| F17 | Endpoint reconcile on reconnect | 10min blackout, 200 local + 150 hub writes; reconciles within 60s; no dups; `global_seq` monotonic | M | F10, F16 | qwen3-coder:480b-cloud |
| F18 | `mcctl endpoint list/show/revoke/tail` | Table + JSON outputs; revoke flips key; endpoint 401s <2s | M | F02, F05 | deepseek-v3.2:cloud |
| F19 | `mcctl push-policy / push-persona` | Upload bundle; version row created; endpoint heartbeat shows `persona_version` match within 60s | S | F12, F13 | deepseek-v3.2:cloud |
| F20 | Endpoint docker-compose + bootstrap README | Fresh operator registers endpoint end-to-end <10min on clean Debian | M | F06, F08 | qwen3-coder:480b-cloud |
| F21 | E2E fleet integration test (1 hub + 3 endpoints) | 3 endpoints on 3 Docker networks; task-completion + memory-replication + blackout-recovery suites pass; JUnit XML; CI green | **L** | F16, F17, F20 | qwen3-coder:480b-cloud |
| F22 | Quarantine state | Force api_key expiry during severe-degraded; endpoint enters `mode_state=quarantine`; recovery via `mcctl endpoint unquarantine` | S | F15, F16 | deepseek-v3.2:cloud |
| F23 | Fleet metrics on hub | `/metrics` exposes `fleet_endpoints_total`, `fleet_endpoints_by_state`, `fleet_memory_lag_seconds{endpoint_id}`, `fleet_heartbeat_age_seconds{endpoint_id}`; Grafana dashboard JSON | M | F05 | qwen3-coder:480b-cloud |
| F24 | Endpoint self-report telemetry | Every heartbeat reports load/counters/mode_state; hub stores in `fleet_endpoints.last_telemetry`; `mcctl endpoint show` renders | S | F05 | deepseek-v3.2:cloud |
| F25 | Docs: protocol spec, runbook, threat model | `docs/fleet/{protocol.md, runbook.md, threat_model.md}` checked in; lint clean | M | F17, F22 | hf:openai/gpt-oss-120b:fastest |

**Total:** 25 tickets. S=7, M=15, L=2 (F16, F21). *+4 tickets from multi-tenant amendment (C-26..C-29) below → 29 tickets total, S=9, M=17, L=2.*

### Multi-tenant endpoint support (C-26..C-29)

*Amendment 2026-04-23. C6 flipped to ship multi-tenant in GA. Patterns ported from `salucallc/tiresias-core` tenant-partner isolation (reference: `project_tiresias_partner_portal` memory, tiresias-partner GKE cluster live since 2026-04-07). Runs parallel to existing waves; lands within Epic-C LoE ~30-35h window without extending wall-clock.*

| # | Title | APE/V | Size | Deps | Model |
|---|---|---|---|---|---|
| **C-26** | soul-lite multi-tenant schema + asyncpg query layer with `tenant_id` scoping | See APE/V block below | **M** | E5/SS-11 (asyncpg repo layer) | qwen3-coder:480b-cloud |
| **C-27** | Per-tenant Fleet endpoint auth model: `api_key` tied to `tenant_id`; registration payload carries tenant binding; per-tenant policy enforcement | See APE/V block below | **M** | C-26, TIR-03 (Tiresias tenant identity / soulkey auth middleware) | qwen3-coder:480b-cloud |
| **C-28** | Per-tenant policy bundle entries: bundle manifest keyed by `tenant_id`; signer signs each tenant's bundle individually | See APE/V block below | **S** | C-27 | deepseek-v3.2:cloud |
| **C-29** | Multi-tenant integration assertions folded into F21: two tenants on the same endpoint, memory isolation assertion, cross-tenant policy-leakage test | See APE/V block below | **S** | C-26, C-27, C-28, F21 | deepseek-v3.2:cloud |

#### C-26 — soul-lite multi-tenant schema + asyncpg query layer

**What:** Extend soul-lite (sqlite + WAL) schema: every user-scoped table gets a non-null `tenant_id TEXT` column with index. Build a thin `TenantScopedRepository` helper over the SS-11 asyncpg/aiosqlite abstraction so every read/write path enforces `WHERE tenant_id = :tid` at the repository boundary — callers cannot pass a raw SQL string; they must go through the helper. Migration 0001 (soul-lite) becomes 0001 + 0002 (tenant_id add). Existing rows get a default `tenant_id='__legacy__'` for backward compat in dev fixtures; production endpoints refuse boot if any row has that value.

**APE/V (machine-checkable):**
- `sqlite3 /data/soul-lite.db "PRAGMA table_info(memories);"` shows `tenant_id` column NOT NULL
- `sqlite3 /data/soul-lite.db "SELECT name FROM sqlite_master WHERE type='index' AND sql LIKE '%tenant_id%';"` returns ≥1 index per user-scoped table
- `pytest tests/soul_lite/test_tenant_scope.py -v` green: (a) repository write tagged tenant_a cannot be read when query bound to tenant_b; (b) raw SQL bypass attempt via `conn.execute` raises `RepositoryBoundaryViolation` (lint-checked via static AST scan `scripts/lint_no_raw_sql.py`); (c) 10k writes under two tenants show perfect isolation in `SELECT COUNT(*) GROUP BY tenant_id`
- Migration idempotent: re-run → no error, schema unchanged
- Endpoint boot refuses if `SELECT COUNT(*) FROM memories WHERE tenant_id='__legacy__' > 0` in prod mode (`ENVIRONMENT=production`)

#### C-27 — Per-tenant auth model + registration tenant binding

**What:** Extend Fleet register/rotate flow so every `api_key` is bound to a `tenant_id`. `POST /v1/fleet/register` payload gains `"tenant": {"tenant_id": "...", "tenant_slug": "acme-corp"}`. Hub stores `fleet_endpoint_tenants (endpoint_id, tenant_id, api_key_hash, status)` — one endpoint can have N rows, one per tenant it serves. API-key format: `sk_endpoint_<tenantslug>_<endpoint_id>_<sha256>`. Every inbound Fleet request (heartbeat, memory push, policy pull) resolves `(api_key → tenant_id)` and stamps `tenant_id` on every downstream DB row + log line. Tenant identity flows in from Tiresias TIR-03 (Soulkey auth middleware) — endpoints registered for a tenant must present a soulkey issued under that tenant's scope.

**APE/V (machine-checkable):**
- `mcctl token create --site acme-sfo --tenant acme-corp --ttl 15m` → one-shot token with tenant binding; DB row shows `tenant_id` non-null
- `curl -XPOST /v1/fleet/register` with tenant_id=A → 201 returns `api_key` with `acme-corp` slug; re-register same endpoint with tenant_id=B → 201 second api_key, both active
- `pytest tests/fleet/test_tenant_auth.py -v` green: (a) key for tenant_a cannot read memory scoped to tenant_b (403 `tenant_mismatch`); (b) heartbeat from tenant_a api_key shows `tenant_id=a` in `fleet_endpoints.last_telemetry`; (c) key revocation of tenant_a leaves tenant_b active on same endpoint; (d) soulkey presented under tenant_a scope cannot register endpoint for tenant_b (Tiresias proxy rejects)
- `/v1/fleet/endpoints/{id}` response includes `"tenants": [{"tenant_id": "...", "status": "active", "api_key_expires_at": "..."}]`
- Audit log: every Fleet request emits `tenant_id` field; grep `"tenant_id":null` on audit stream returns zero hits

#### C-28 — Per-tenant policy bundle entries

**What:** Extend `/v1/fleet/policy` so the response is keyed by tenant: `{"policy_version": "...", "bundles": {"tenant_a": {...signed bundle...}, "tenant_b": {...}}}`. Signer (F12) signs each tenant's bundle separately with the same ed25519 key (v1 GA; per-tenant signing keys deferred to v1.1). Endpoint applies the tenant-scoped bundle at tool-invocation time based on the calling tenant. `mcctl push-policy --tenant acme-corp` pushes only for that tenant. `mesh_scope.allowed_topics_prefix` becomes per-tenant so tenant_a's prefix (`acme-sfo.`) never authorizes tenant_b's topics.

**APE/V (machine-checkable):**
- `GET /v1/fleet/policy?endpoint_id=...` returns `bundles` object with N entries (one per registered tenant)
- `openssl dgst -verify hub_pub.pem -signature <sig> <bundle_json>` passes independently for each tenant's bundle
- `mcctl push-policy --tenant acme-corp --bundle new.yaml` → only acme-corp's `policy_version` bumps; tenant_b's version unchanged
- `pytest tests/fleet/test_tenant_policy.py -v` green: (a) tenant_a's `tool_allowlist=["mcp.github.read"]` and tenant_b's `tool_allowlist=["mcp.linear.write"]`; tenant_a call to `mcp.linear.write` → 403 `tool_not_in_tenant_allowlist`; (b) tampering tenant_a's bundle (flip one byte) → signature verify fail, endpoint logs `policy.tenant_bundle.invalid_sig` and keeps previous version for that tenant only (tenant_b unaffected)
- Heartbeat `sync_cursor.policy_version` becomes `{"tenant_a": "1.0.1", "tenant_b": "1.0.0"}`

#### C-29 — Multi-tenant integration assertions folded into F21

**What:** Extend F21's 1-hub-3-endpoints E2E suite with a multi-tenant scenario: one of the three endpoints hosts two tenants (acme-corp + beta-industries). Suite adds three new assertion classes: (1) **memory isolation** — tenant_a writes `tenant_a.secret`, tenant_b's search returns zero rows; (2) **cross-tenant policy leakage** — tenant_a has `tool_allowlist=[mcp.github.read]`, tenant_b has `tool_allowlist=[]`; tenant_b attempt at `mcp.github.read` blocked with `tool_not_in_tenant_allowlist` even on the same endpoint; (3) **blackout recovery with mixed tenants** — 10-minute hub blackout; both tenants buffer locally; on reconnect, reconcile order preserves per-tenant `global_seq` independence (tenant_a's seq doesn't consume tenant_b's slots).

**APE/V (machine-checkable):**
- `pytest tests/fleet/e2e/test_multitenant.py -v` green on all three assertion classes; results added to F21 JUnit XML
- CI run shows `fleet_multitenant_*` labels; Grafana panel (wave-3 follow-on, not in this ticket) can render per-tenant memory lag
- Cross-tenant grep: `jq '.tenant_id' e2e_audit.jsonl | sort -u | wc -l` → exactly 2 (only acme-corp + beta-industries on the mixed endpoint; no stray null or '__legacy__')
- Blackout recovery: after reconnect, `SELECT tenant_id, MAX(local_seq) FROM fleet_memory_sync_log GROUP BY tenant_id` shows monotonic per-tenant sequences; no gaps; no interleave corruption
- Entire F21 suite (original + multi-tenant additions) runs under 15min wall-clock on CI

**Multi-tenant amendment summary:**
- +4 tickets (C-26/M, C-27/M, C-28/S, C-29/S), ~30-35h LoE
- Runs parallel to Waves 1-3 on existing implementer slots (no new concurrency needed; absorbs into the Fleet epic's 2-3 worker cap)
- Critical-path impact: C-29 joins F21 in Wave 3 but does not extend wall-clock (F21 is already the Wave 3 long pole)
- Cost impact: absorbed within Epic C's existing $1-2 budget; per-tenant ed25519 signing in C-28 is zero marginal $
- MSSP story: one appliance, N tenants, cryptographic isolation at auth + repo + policy-bundle layers; PQ (SS-08) + multi-tenant layered = per-tenant hybrid-signed receipts in v1.1

## 6. Dependency Graph + Waves

```
Wave 1 (day 1-3):    F01, F07, F08       parallel
Wave 2 (day 3-6):    F02, F03 after F01; F04 after F02
Wave 3 (day 6-10):   F05, F06, F09, F12  parallel
Wave 4 (day 10-14):  F10, F13, F15, F18, F23
Wave 5 (day 14-18):  F11, F14, F16, F17, F19, F20, F22, F24
Wave 6 (day 18-21):  F21 (integration), F25 (docs)
```

Critical path: F01 → F02 → F04 → F05 → F06 → F09 → F10 → F17 → F21. **~3 weeks with 2-3 concurrent agents.**

## 7. Risk Register

| # | Risk | P | Impact | Mitigation |
|---|---|---|---|---|
| R1 | **Split-brain** — double-claim during transient disconnect | Med | High | Claim scoped to `mesh_scope.allowed_personas`; idempotent on task_id; hub 409 on dup; `claim_id` cookie on completes drops stale. APE/V in F17 |
| R2 | **Stolen api_key** | Med | Critical | 24h rotation (F15); revoke <2s (F18); writes scoped to topic prefix; ed25519 bundle verify; identity.json mode 0600; quarantine (F22); audit log |
| R3 | **Replication conflicts on singletons** | Med | High | Append-only global-seq log; `fleet.conflict` journal (F11); `mcctl endpoint conflicts` for operator decision |
| R4 | **Degraded surprise** — operator expects tool work | High | Med | Every tool's degraded behavior in policy + 503 body; APE/V matrix F16 (8 cases); visible in heartbeat + CLI + Grafana (F23); per-tool fail-closed (default) vs fail-open (explicit opt-in) |
| R5 | **Unbounded local queue** | Med | High | Capped 10k in-memory → spills to `pending_push.jsonl`; hard 1GB disk → quarantine; `mcctl endpoint drain --strategy=drop-oldest\|snapshot-and-clear`; metrics alert at 50/80/95% |

## 8. Cost Estimate

### Model $
- qwen3-coder:480b-cloud on Ollama Max (flat $100/mo already budgeted) — 15 tickets
- deepseek-v3.2:cloud — 7 tickets × ~$0.049 = **$0.35**
- hf:openai/gpt-oss-120b:fastest — F25 docs, **$0** free tier
- Review passes (deepseek) — +50% overhead = **$0.50**

**Total: ~$1-2 for the epic.**

### Runtime per endpoint (steady state)
- Heartbeat 15s cadence × 86400 × ~800B = ~4.6 MB/day ingress
- Memory push ~500 writes/day × 2KB = ~1 MB/day
- Memory pull ~0.5 MB/day
- WS near-zero CPU when idle
- DB ~6000 rows/day in `fleet_memory_sync_log`; 90-day trim
- Hub cost at <1000 endpoints: **effectively $0**
- First bottleneck: fleet-gateway WS fan-out at ~5000 concurrent — well beyond GA

## 9. Cross-Epic Touchpoints

| Epic | Fleet needs | Fleet exposes |
|---|---|---|
| **Tiresias** | Each endpoint may wrap local Tiresias; needs `endpoint_id` on audit rows for per-site scoping | Endpoint advertises Tiresias URL in heartbeat; hub aggregates with `endpoint_id`; Tiresias epic must add `endpoint_id` to schema |
| **Aletheia** | Per-endpoint seed material; endpoint bundles could include Aletheia key split | Endpoints contribute local evidence; `fleet_memory_sync_log.content_hash` becomes Merkle leaf input |
| **Ops** | Grafana infra, alert routing, on-call runbook pattern; metrics (F23) plug in | 3 new Grafana panels + 3 alerts (endpoint down >2min, memory_lag >300s, quarantine count >0) |
| **Soul gap-closure** | `persona` field currently hardcoded `alfred`; endpoints need accurate attribution | Endpoint writes carry `source.persona` explicitly; soul gap-closure must honor instead of substituting |
| **Portal (V0.5)** | Read-only "Endpoints" tab — not this epic | `DataPort.listEndpoints()` sketched; hub `/v1/fleet/endpoints` API ready for consumption |

## 10. Prominent Flag for Cristian

**Network topology LOCKED to endpoint-initiated WSS.** If any pilot customer's security team forbids outbound WebSocket (some shops restrict persistent outbound too), fall back to HTTP long-poll. Handled at transport layer in F04/F06, shouldn't leak upward — but if discovered late in deployment, per-endpoint transport-selection flag needs carve-out in policy bundle. **Add this as open decision O7 before kicking off F04.**
