# D. Ops Layer — Cost + Observability + Auth/RBAC + Backup + Secrets

*Epic owner: TBD · Mission Control v1.0 GA · Drafted 2026-04-23 (Plan sub D)*

## 1. Epic summary

Today the appliance stands up and services talk; tomorrow it has to be operable. This epic adds the five boring-but-mandatory ops primitives (cost accounting, observability, auth/RBAC, backup/restore, secret vault) in a single wave so they share one compose-infrastructure deployment rhythm rather than five separate ones. The wave adds six new containers (`otel-collector`, `prometheus`, `loki`, `grafana`, `authelia`, `infisical`) on a new `mc-ops` network, plus compose-native volume snapshotting via restic. The target is a GA-grade appliance a regulated customer can actually run — someone can see what it's doing, pay for what it spends, log in with their own IdP, restore from yesterday, and rotate a secret without SSH. **28 tickets, 6 waves, ~12 days critical path, $0 execution cost (absorbed by Ollama Max + free tier).**

## 2. Architecture

### 2.1 New services added to compose

| Service | Image (pinned) | Purpose | Ports (loopback only) |
|---|---|---|---|
| `otel-collector` | `otel/opentelemetry-collector-contrib:0.115.0` | OTLP receiver → fan out to prometheus + loki | 4317 (grpc), 4318 (http) internal |
| `prometheus` | `prom/prometheus:v2.55.1` | Metrics TSDB (30d retention) | — |
| `loki` | `grafana/loki:3.3.2` | Log aggregation (14d retention, filesystem backend) | — |
| `grafana` | `grafana/grafana-oss:11.4.0` | Dashboards (no custom UI) | Exposed via Caddy `/ops/*` |
| `infisical` | `infisical/infisical:v0.124.0-postgres` | Self-hosted secret vault (uses `mc-postgres`, dedicated schema) | — |
| `authelia` | `authelia/authelia:4.38.17` | OIDC broker + RBAC + session mgmt | — |

Plus **infisical-agent sidecar** pattern on every existing service (lightweight, ~20MB).

### 2.2 Integration contract

```
┌─────────────┐    metrics    ┌──────────────┐
│ soul-svc    │──────────────▶│ otel-collect │──▶ prometheus ──▶ grafana
│ tiresias    │──OTLP/grpc───▶│              │──▶ loki       ──▶ grafana
│ alfred-coo  │──────────────▶│              │
│ portal      │──logs+traces─▶│              │
└─────────────┘               └──────────────┘
      │ auth                          ▲
      ▼                               │ scrape
┌─────────────┐   OIDC/SAML    ┌──────┴───────┐
│ caddy (edge)│──────────────▶│ authelia     │──▶ customer IdP
└─────────────┘                └──────────────┘
      │
      ▼
┌─────────────┐   secret fetch  ┌──────────────┐
│ all services│───HTTP/mTLS────▶│ infisical    │──▶ postgres (infisical schema)
└─────────────┘                 └──────────────┘

Backup: restic cron → /backups mount (+ optional S3/B2/GCS)
```

Caddy gets new path routes: `/ops/*` → grafana, `/auth/*` → authelia, `/vault/*` → infisical UI. All behind authelia `forward_auth`.

## 3. Five sub-area plans

### 3.1 Cost + Token Accounting

**Goal.** Every inference call attributable to `{tenant_id, persona_id, session_id, provider, model}` with tokens and USD cost; budget alerts → `#batcave`.

**Solution.** Extend existing `tiresias_audit_log` (Tiresias already captures per-turn tokens). Add:
- `tiresias.cost_usd` computed column fed by `config/model_pricing.yaml` (Ollama Max = flat $100/mo amortized; free tiers = $0; paid providers = per-token table)
- Prometheus exporter `tiresias-cost-exporter` (150-LOC Go) scrapes audit log every 60s; emits `mc_tokens_total{...}` + `mc_cost_usd_total{...}` gauges
- Alertmanager: per-tenant daily budget (default $10), per-persona monthly budget (default $50) → Slack webhook to `#batcave`

**Primary KPI: "% of turns on free tier"** — regression there is the real alert.

### 3.2 Observability

**Solution.** Prometheus + Loki + Grafana (Cristian's brief rules out Cortex/Thanos/ES). OTEL collector receives pushes. **Tempo/tracing deferred to v1.1** — few users will look at traces in first 90 days.

4 pre-provisioned Grafana dashboards:
1. **Appliance Health** (one-pager: services up/down, disk, RAM, CPU, pg connections)
2. **Cost & Tokens**
3. **Soul Activity** (memory writes/s, retrieval p95)
4. **Auth & Access** (login success/fail, who-saw-what audit)

Retention: 30d Prom + 14d Loki = ~15GB disk.

### 3.3 Auth / RBAC

**Solution.** Authelia 4.38.17.
- vs Dex: Authelia ships RBAC + session UI + TOTP/WebAuthn out-of-box
- vs Keycloak: Keycloak wants 1GB+ RAM; overkill for single-host
- Local fallback (air-gap): file-based user backend, `admin` passphrase set in wizard Screen 8
- BYO IdP via `authelia/configuration.yml` OIDC clients

**RBAC v1 (minimum viable):**
- `appliance-admin`: full access to `/ops/*`, `/vault/*`, `/auth/*`, all backend APIs
- `tenant-user`: `/`, `/soul/*` read+write own tenant, `/chat/*`
- Sovereignty-admin (KEK holder) authenticated separately via Asphodel challenge

**Scoped API keys** replace shared bearer: OAuth2 client credentials, scopes `soul:memory:read/write`, `tiresias:audit:read`, `tiresias:cost:read`, `mcp:tools:invoke`, `vault:read/write`. 24h TTL, portal rotation.

### 3.4 Backup / Restore

**Solution.** Restic 0.17.3 in cron sidecar.
- vs pgBackRest: Postgres-only; we need volume-level (soul-data, audit, vault, kek-envelope)
- vs Borg: no S3/B2/GCS native
- Backends: `local` (default), `s3-compatible` (S3/B2/Wasabi/MinIO), `rclone` (GDrive/OneDrive/Dropbox)
- Schedule: nightly 03:00 local; 7 daily + 4 weekly + 6 monthly retention
- Encryption: Restic's AES-256; repo password derived from Asphodel KEK (sovereignty property preserved)
- Restore: `./mc.sh restore --snapshot <id>` → RTO 20min
- DR runbook: `deploy/appliance/DISASTER_RECOVERY.md` with 3 scenarios (disk failure, whole-host, ransomware)

### 3.5 Secret Management

**Solution.** Infisical v0.124.0 (MIT-licensed, self-hosted Postgres backend).
- vs HC Vault: BSL 1.1 license conflicts; most compliance-sensitive customers prefer non-BSL
- vs OpenBao (Vault fork): ecosystem still maturing in April 2026
- Offline mode works (point at local postgres, disable SSO)

**Migration from `./state/secrets/*`:**
- On-disk files remain as fallback readable only by init container at boot
- Init pushes them into infisical the first time, then chmod 000 (disabled-but-recoverable)
- Services gain infisical-agent sidecar (~20MB) writing secrets to tmpfs `/run/secrets/`
- **Zero code changes in soul-svc / tiresias / portal**
- Rotation: `POST /api/v3/secrets/:id/rotate` → agents pick up next poll (60s) → services restart if `requires_restart:true`

Appliance vault is isolated — no cross-sync with GCP SM. Customer DWD JSON provided via Infisical UI at install.

## 4. Decisions

### Locked

| Sub-area | Decision |
|---|---|
| Cost | Extend `tiresias_audit_log`, YAML pricing table, `#batcave` alerts |
| Obs | Prom + Loki + Grafana; OTEL collector; no Tempo in v1 |
| Obs | 30d Prom, 14d Loki retention |
| Auth | Authelia (not Dex/Keycloak); 2-tier RBAC only |
| Auth | File backend fallback; BYO OIDC via Settings |
| Backup | Restic (not pgBackRest/Borg); KEK-derived repo password |
| Secrets | Infisical (not HC Vault/OpenBao); agent sidecars, no code changes |
| All | Ops services loopback-only, Caddy path-routed |

### Open (Cristian's call)

1. **Preflight disk bump.** Restic ~200MB/day × 6-month retention = ~36GB. Current 40GB preflight leaves 4GB for Prom+Loki. **Recommend: bump to 60GB for v1 GA.**
2. **Pricing table authority.** Who updates `model_pricing.yaml` when OpenRouter changes prices? **Recommend: monthly cron pulls OpenRouter pricing JSON, opens PR.**
3. **Authelia admin passphrase.** Reuse Screen 3 KEK passphrase or force second one? **Recommend: force second passphrase (data key vs auth key separation).**
4. **Budget alert defaults.** $10/tenant/day and $50/persona/month are guesses. Ask Cristian for Ollama Max real numbers.
5. **Tracing.** Defer to v1.1 as recommended, or squeeze Tempo into v1 GA? **Recommend: defer.**
6. **Offsite backup default.** Local-only default; customers enable S3/B2 in Settings. Confirm or offer Saluca-provided B2 upsell? (GTM question.)

## 5. Combined ticket breakdown (28 tickets, 6 waves)

### Wave 1 — Infrastructure foundation
| # | Title | Sub | APE/V | Effort | Deps | Model |
|---|---|---|---|---|---|---|
| 1 | SAL-OPS-01: mc-ops network + volumes | Obs | `docker compose config` exits 0; volumes created | S | — | deepseek-v3.2:cloud |
| 2 | SAL-OPS-02: Pin all image versions in IMAGE_PINS.md | All | `grep :latest docker-compose.yaml` returns 0 matches | S | — | deepseek-v3.2:cloud |
| 3 | SAL-OPS-03: Caddy routes /ops /auth /vault | Auth/Obs | `curl /ops/` returns 401 or 302 to /auth | S | 1 | qwen3-coder:480b-cloud |

### Wave 2 — Secrets (blocks everything else)
| # | Title | APE/V | Effort | Deps | Model |
|---|---|---|---|---|---|
| 4 | SAL-OPS-04: Infisical service + pg schema | `curl infisical:8080/api/status` returns ok; psql shows `infisical` schema with >5 tables | M | 1 | qwen3-coder:480b-cloud |
| 5 | SAL-OPS-05: KEK-derived infisical root key | Restart 2x → secrets readable; DERIVED_FROM_KEK marker file | M | 4 | qwen3-coder:480b-cloud |
| 6 | SAL-OPS-06: Agent sidecar for soul-svc | `cat /run/secrets/soul_svc_jwt` matches infisical UI value | M | 5 | qwen3-coder:480b-cloud |
| 7 | SAL-OPS-07: Agent sidecars for tiresias/portal/mcp-core | Same for each service | M | 6 | qwen3-coder:480b-cloud |
| 8 | SAL-OPS-08: Migrate `./state/secrets/*` → infisical | Delete state dir, restart → services healthy, secrets in UI | M | 6, 7 | deepseek-v3.2:cloud |
| 9 | SAL-OPS-09: Rotation endpoint + docs | POST rotate → new value; services pick up within 90s | S | 8 | deepseek-v3.2:cloud |

### Wave 3 — Auth
| # | Title | APE/V | Effort | Deps | Model |
|---|---|---|---|---|---|
| 10 | SAL-OPS-10: Authelia + file backend | /auth/ renders; admin login returns 200 | M | 3, 7 | qwen3-coder:480b-cloud |
| 11 | SAL-OPS-11: Caddy forward_auth for /ops/* | Unauth → 302 /auth; session cookie → 200 | S | 10 | deepseek-v3.2:cloud |
| 12 | SAL-OPS-12: BYO OIDC template + docs | `./mc.sh auth add-oidc google --client-id X` adds config; test IdP login works | M | 10 | qwen3-coder:480b-cloud |
| 13 | SAL-OPS-13: RBAC groups | tenant-user → 403 on /ops; appliance-admin → 200 | M | 10 | qwen3-coder:480b-cloud |
| 14 | SAL-OPS-14: Scoped OAuth2 API tokens | soul:memory:read → 200 on search, 403 on write | L | 13 | qwen3-coder:480b-cloud |
| 15 | SAL-OPS-15: Wizard Screen 8 — admin + RBAC init | Post-wizard admin exists; `./mc.sh auth list-users` ≥1 row | M | 10 | qwen3-coder:480b-cloud |

### Wave 4 — Observability (parallel to Wave 3)
| # | Title | APE/V | Effort | Deps | Model |
|---|---|---|---|---|---|
| 16 | SAL-OPS-16: OTEL collector + receivers | `curl otel-collector:4318/v1/traces -d '{}'` → 200 | M | 1 | qwen3-coder:480b-cloud |
| 17 | SAL-OPS-17: Prometheus + scrape config | `/api/v1/targets` shows ≥5 `up=1` targets | M | 1 | deepseek-v3.2:cloud |
| 18 | SAL-OPS-18: Loki + docker log-driver | `query={container="mc-soul-svc"}` returns non-empty | M | 1 | qwen3-coder:480b-cloud |
| 19 | SAL-OPS-19: Grafana + datasource provisioning | `/api/datasources` returns prometheus + loki | S | 17, 18 | deepseek-v3.2:cloud |
| 20 | SAL-OPS-20: 4 provisioned dashboards | All 4 uids resolve; mc-health green on clean install | M | 19 | hf:openai/gpt-oss-120b:fastest |
| 21 | SAL-OPS-21: Alertmanager + #batcave webhook | Test alert → message in #batcave within 30s | S | 17 | deepseek-v3.2:cloud |

### Wave 5 — Cost accounting
| # | Title | APE/V | Effort | Deps | Model |
|---|---|---|---|---|---|
| 22 | SAL-OPS-22: Schema migration 019 — cost_usd | `\d tiresias_audit_log` shows `cost_usd numeric(10,6)` | S | 1 | deepseek-v3.2:cloud |
| 23 | SAL-OPS-23: model_pricing.yaml + loader | `pricing.load()['openrouter/free']['input_per_1k']` returns 0.0 | S | 22 | deepseek-v3.2:cloud |
| 24 | SAL-OPS-24: tiresias-cost-exporter (Go, ~150 LOC) | `/metrics` returns mc_cost_usd_total + mc_tokens_total with tenant/persona labels | M | 17, 23 | qwen3-coder:480b-cloud |
| 25 | SAL-OPS-25: Budget alert rules | $15 test row → #batcave alert within 120s | S | 21, 24 | deepseek-v3.2:cloud |

### Wave 6 — Backup/restore
| # | Title | APE/V | Effort | Deps | Model |
|---|---|---|---|---|---|
| 26 | SAL-OPS-26: Restic + nightly cron | `restic snapshots` shows ≥1 within 24h | M | 1, 5 | qwen3-coder:480b-cloud |
| 27 | SAL-OPS-27: `./mc.sh restore` flow | Tampered volume recovers; smoke_test.sh passes after | L | 26 | qwen3-coder:480b-cloud |
| 28 | SAL-OPS-28: DR runbook + stale-alert | DISASTER_RECOVERY.md exists (3 scenarios); 36h stale alert fires | S | 20, 21, 26 | hf:openai/gpt-oss-120b:fastest |

## 6. Dependency graph

```
Wave 1 (1,2,3) ──┐
                 │
Wave 2 (4→5→6→7→8→9) secrets, serial, ~3 days
                 │
      ┌──────────┴──────────┐
      │                     │
   Wave 3 (auth)         Wave 4 (obs) ← parallel, ~3 days each
      │                     │
      │                 Wave 5 (cost, 22→23→24→25) ~2 days
      │                     │
      └──────────┬──────────┘
                 │
            Wave 6 (backup, 26→27→28) ~2 days
```

After Wave 2 completes, Waves 3+4 run fully parallel (max 4 concurrent agents). Critical path: **~12 days**.

## 7. Risk register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | Authelia forward_auth breaks portal Supabase SSR cookies | Med | High | Feature flag `AUTH_MODE=authelia|supabase|bypass`; keep Supabase path through v1 GA; cut over at v1.1 |
| 2 | Losing KEK locks out vault AND every service | Med | Critical | Wizard Screen 4 mnemonic recovery (existing); add recovery-from-mnemonic test in smoke_test.sh |
| 3 | Prom+Loki disk growth exceeds preflight | Med-High | Med | Bump preflight to 60GB; Grafana panel alerts at 80%; auto-prune oldest Loki chunks at 85% |
| 4 | Schema migration 019 breaks audit rows | Low | High | ADD COLUMN...DEFAULT 0 (non-breaking); rollback script; snapshot before apply |
| 5 | qwen3 weaker at Go than Python (tiresias-cost-exporter is only Go ticket) | Med | Med | Primary qwen3-coder:480b, fallback deepseek-v3.2; template from existing mcp-gateway exporters |

## 8. Cost estimate

### Model spend (12-18 day build)
| Model | Tickets | Tokens (M) | Cost |
|---|---|---|---|
| deepseek-v3.2:cloud | 11 S/small-M | ~8M in, 2M out | $0 (Ollama Max flat) |
| qwen3-coder:480b-cloud | 14 M/L | ~18M in, 4M out | $0 (Ollama Max flat) |
| hf:openai/gpt-oss-120b | 3 prose-heavy | ~3M in, 1M out | $0 (HF free) |
| OpenRouter free fallback | ~6 retries | ~2M | $0 |
| **Total execution** | | | **$0** |

### Runtime infra added
| Resource | Monthly | Notes |
|---|---|---|
| Prom TSDB 30d | $0 | ~3GB local |
| Loki 14d | $0 | ~12GB local |
| Restic local | $0 | customer disk |
| Restic B2 opt-in (36GB) | ~$0.50 | customer pays |
| Infisical, Authelia, Grafana OSS | $0 | self-hosted |
| **Total Saluca-side** | **$0** | |

**Customer disk:** bump preflight 40GB → 60GB.

## 9. Cross-epic touchpoints

### What other epics need from Ops
| Epic | Need | Delivered |
|---|---|---|
| **Tiresias** | Cost-per-tenant visibility | #22, #24 |
| **Aletheia** | RBAC audit log for who-saw-what | #13, #20 |
| **Fleet** | Health-check endpoint + metrics scrape format | #17 standardized /metrics |
| **Soul** | Memory-write latency metrics + dream-cycle cost | #16, #24 |

### What Ops needs
| From | Need | Blocker? |
|---|---|---|
| Stream F (license) | License lock before Infisical image with Saluca branding | Soft — ship neutral branding v1 |
| Stream B (appliance-B) | Wizard Screen 8 slot between 7 and "Open MC" | Hard — coordinate with portal team |
| Tiresias | `tiresias_audit_log` row format stable | Hard — migration 019 assumes current columns |
| Soul | `/metrics` endpoint on soul-svc | Med — if absent, #16 effort grows to add it |
