# A. Tiresias-in-Appliance — Policy Proxy + AHI Constitutional Principles

*Epic owner: TBD · Mission Control v1.0 GA · Drafted 2026-04-23 (Plan sub A)*
*Linear: SAL-TIR-01..15 · Target: v1.0.0-rc.1*

## 1. Epic summary

**Goal.** Insert a sovereign Tiresias policy proxy into the appliance docker-compose so every AI tool-call and egress request flows through an auditable PDP that enforces the 12 AHI Constitutional Principles and the dual-key soulkey identity model. Post-merge, `alfred-coo-svc` and `open-webui` cannot reach `api.github.com` / `slack.com` / `linear.app` / `notion.com` / `api.anthropic.com` / `api.openai.com` directly; all traffic is mediated by `tiresias-proxy`, which re-signs with per-service soulkeys and routes to the curated `mcp-*` services that hold real tokens.

**In scope:** (a) new `tiresias-proxy` container (sovereign single-binary, stateless, Postgres-backed audit); (b) 12 principles in 4 categories (Cedar/Rego-lite — hash-chain principle revisions); (c) soulkey minting for each MCP service + COO daemon with SHA-512 hash at rest; (d) egress allowlist via split docker networks (COO loses direct internet); (e) audit/CoT chain table; (f) healthz + policy-list endpoints.

**Non-goals:** No Tiresias admin UI beyond JSON. No SoulWatch anomaly detection (v1.1). No multi-tenancy inside single appliance. No Stripe/billing. No Cedar PDP editor UI. No Aletheia GVR loop (audit hash-chain only here).

**Win condition:** drop `iptables -A OUTPUT -p tcp -d api.github.com -j REJECT` on the COO netns; `alfred-coo-svc` still successfully opens a GitHub issue because the request re-routed `coo → tiresias → mcp-github → api.github.com`; the `_soulauth_audit` row shows `principle_check=pass`, valid `soulkey_sha512`, valid `prev_hash`.

## 2. Architecture

### 2.1 New traffic flow

```
                              ┌────────────────────────────────────┐
                              │  Appliance host (docker-compose)   │
                              │                                     │
  ┌─────────┐   :443/:80      │   ┌──────────┐                     │
  │ Browser │────────────────────▶│  caddy   │ (mc-edge)           │
  └─────────┘                  │   └────┬─────┘                     │
                              │        │ path-routes                │
                              │        ├── /soul/*      ─▶ soul-svc │
                              │        ├── /tiresias/*  ─▶ tiresias │
                              │        ├── /ui/*        ─▶ open-webui│
                              │        ├── /portal/*    ─▶ portal   │
                              │        └── /coo/*       ─▶ coo      │
                              │                                     │
                              │ ══════ mc-internal (bridge) ══════  │
                              │                                     │
                              │  ┌──────────────┐                   │
                              │  │ alfred-coo   │ NEW: all egress   │
                              │  │ alfred-portal│      goes to      │
                              │  │ open-webui   │      tiresias:8840│
                              │  │ soul-svc     │                   │
                              │  └──────┬───────┘                   │
                              │         │ HTTP + soulkey header     │
                              │         ▼                           │
                              │   ┌──────────────┐  PDP: 12         │
                              │   │  tiresias-   │  principles,     │
                              │   │  proxy:8840  │  soulkey auth,   │
                              │   │  (sovereign) │  audit chain     │
                              │   └──────┬───────┘                  │
                              │          │ allowed → upstream       │
                              │          │ denied → 403 + audit     │
                              │          ▼                           │
                              │   ┌────────────────┐                │
                              │   │ mcp-github     │ ─▶ api.github  │
                              │   │ mcp-slack      │ ─▶ slack.com   │
                              │   │ mcp-linear     │ ─▶ linear.app  │
                              │   │ mcp-notion     │ ─▶ notion.com  │
                              │   │ mcp-llm (new)  │ ─▶ anthropic/  │
                              │   │                │   openai/ollama│
                              │   └────────────────┘                │
                              │                                     │
                              │  ═══ mc-egress (mcp-* + caddy) ═══  │
                              └────────────────────────────────────┘

KEY CHANGE: tokens (GITHUB_TOKEN, SLACK_BOT_TOKEN, etc.) REMOVED from
alfred-coo-svc env. Only mcp-<service> containers hold them.
```

### 2.2 Key components

| Component | Type | Image | Port | Networks |
|---|---|---|---|---|
| `tiresias-proxy` | new | `ghcr.io/salucallc/tiresias-sovereign:v1.0.0` | 8840 internal | mc-internal only |
| `mc-egress` | new docker bridge | — | — | caddy, mcp-github/slack/linear/notion/llm, tiresias |
| `mc-internal` | existing | — | — | all services; **no internet route after this epic** |
| `tiresias_audit` schema | new in Postgres | — | — | owned by `tiresias_proxy` role |
| `mcp-llm` | new MCP cascade | `ghcr.io/salucallc/mcp-gateway-node:latest` | 8220 | mc-internal + mc-egress |

### 2.3 Principle enforcement (v1 "Principles-Lite")

12 principles → 4 categories → JSON rules with `{id, category, predicate, action, severity}`:

- **Identity (P1-P3):** soulkey present + valid hash; dual-key match; agent registered
- **Boundary (P4-P6):** destination in allowlist; no proxy loops; no credential echo
- **Accountability (P7-P9):** audit chain append with prev_hash; CoT hash capture; retention
- **Transparency (P10-P12):** `X-Tiresias-Principles-Passed` header; deny reason exposed; versioned policy bundle hash

Full Cedar PDP + operator UI → v1.1. v1 ships a compiled rule evaluator (Rego via OPA sidecar vs hand-rolled Go/Rust — decision §3).

## 3. Decisions

### Locked

| Decision | Value | Why |
|---|---|---|
| Single binary, stateless proxy | 57→1 container pattern from onprem-rebuild | Consistency with GKE sovereign build |
| Audit store | Local Postgres (`tiresias_audit` schema in appliance postgres) | Avoids new volume; reuses backup surface |
| Principle versioning | SHA-256 hash-chained `principle_registry.json` embedded in image | Matches MASTER_K dual-key pattern |
| Soulkey format | `sk_agent_appliance_<service>_<sha256_tail64>` | Matches `reference_agent_fleet_canonical` |
| Soulkey mint | `mc-init` generates one per consumer on first boot, writes to `state/secrets/`, SHA-512 hash in `_soulkeys` table | Reuses existing init pattern |
| v1 GA defers | SoulWatch, Aletheia GVR, SoulGate | Scope cut |
| Image | `ghcr.io/salucallc/tiresias-sovereign:v1.0.0` via new repo | Keeps GKE path untouched |
| Default policy mode | `enforce` (deny-by-default on unknown) | Sovereignty thesis |
| Network topology | Two-network split (`mc-internal` no-egress, `mc-egress` mcp-only) | Only way to prove COO can't bypass |

### Open — needs Cristian's call (with defaults)

1. **Policy engine: OPA/Rego sidecar vs embedded Go evaluator.** *Recommend: embedded.* Keeps "1 container" promise; Rego's power isn't needed for 12 static rules.
2. **Fork tiresias-core vs greenfield `tiresias-sovereign` repo.** *Recommend: greenfield*, vendoring only proxy + PDP from tiresias-core. Forking drags in Cloud SQL code.
3. **Retention default.** *Recommend: 365 days* (matches existing `TIRESIAS_RETENTION_DAYS`).
4. **Deny-response format.** *Recommend: RFC 7807 Problem+JSON* with `type=https://saluca.com/principles/<id>`.
5. **Audit webhook.** *Recommend: defer to v1.1.* Epic D (central hash sync) owns.

If Cristian silent-approves within 24h of kickoff, mesh proceeds.

## 4. Ticket breakdown (15 tickets)

### Wave 1 — Parallel after TIR-01
- **SAL-TIR-01** — Scaffold `salucallc/tiresias-sovereign` repo + CI (S, kickoff, `qwen3-coder:480b-cloud`)
  - APE/V: Repo exists; `docker pull ghcr.io/salucallc/tiresias-sovereign:main` + `/healthz` returns 200 with `"ok":true`
- **SAL-TIR-02** — Embed `principle_registry.json` + hash-chain loader (M, dep TIR-01, `deepseek-v3.2:cloud`)
  - APE/V: 12 entries, 4 categories; `TestPrincipleChainIntegrity` passes (every `prev_hash` = SHA-256 of prior canonical JSON); `/v1/policies` returns `bundle_sha256` + principles[12]
- **SAL-TIR-07** — DB migrations for tiresias_audit schema (S, dep TIR-01, `deepseek-v3.2:cloud`)
  - APE/V: `migrations/001_init.up.sql` creates schema + 4 tables; golang-migrate up/down idempotent; embedded migration runs on boot
- **SAL-TIR-08** — Add `mcp-llm` cascade router (M, dep TIR-01, `qwen3-coder:480b-cloud`)
  - APE/V: mcp-llm on port 8220 with anthropic→openai→ollama cascade; smoke `curl chat/completions` returns 200 non-empty

### Wave 2 — Sequential core proxy
- **SAL-TIR-03** — Soulkey auth middleware (identity P1-P3) (M, dep TIR-02, `deepseek-v3.2:cloud`)
  - APE/V: missing/malformed → 401; unregistered → 403; valid → 200; table-driven test
- **SAL-TIR-04** — Proxy handler + destination allowlist (boundary P4-P6) (M, dep TIR-03, `qwen3-coder:480b-cloud`)
  - APE/V: allowlist table forwards or 403s; `/proxy/mock/ping` returns `pong` via mock upstream
- **SAL-TIR-05** — Audit hash-chain writer (accountability P7-P9) (M, dep TIR-04, `deepseek-v3.2:cloud`)
  - APE/V: every request writes audit row; 100 sequential requests → chain integrity walk passes
- **SAL-TIR-06** — Transparency headers (P10-P12) (S, dep TIR-04/05, `deepseek-v3.2:cloud`)
  - APE/V: `X-Tiresias-Principles-Passed`, `X-Tiresias-Policy-Bundle`, `X-Tiresias-Audit-ID` on all responses; `X-Tiresias-Deny-Reason` on denies

### Wave 3 — Parallel after TIR-09
- **SAL-TIR-09** — Wire into appliance compose (S, dep TIR-01..08, `deepseek-v3.2:cloud`)
  - APE/V: `/tiresias/healthz` and `/tiresias/policies` accessible via Caddy; policies returns `.principles | length == 12`
- **SAL-TIR-10** — Split docker networks (M, dep TIR-09, `qwen3-coder:480b-cloud`)
  - APE/V: `docker exec alfred-coo curl --max-time 5 https://api.github.com` fails with DNS/conn-refused; `docker exec mcp-github curl` same URL succeeds
- **SAL-TIR-12** — mc-init mints soulkeys + registers allowlist (M, dep TIR-07/09, `deepseek-v3.2:cloud`)
  - APE/V: 6 soulkeys generated idempotently; `_soulkeys` rowcount=6; `_soulkey_allowlist` for coo has ≥4 rows
- **SAL-TIR-13** — Update open-webui to route through tiresias (S, dep TIR-11/12, `deepseek-v3.2:cloud`)
  - APE/V: chat completion via browser works; audit_chain row increments per turn

### Wave 4 — Sequential finalize
- **SAL-TIR-11** — Remove raw tokens from coo-svc; wire SOULKEY + TIRESIAS_URL (**L, dep TIR-09/10, `qwen3-coder:480b-cloud`** — critical path)
  - APE/V: grep image filesystem for `ghp_|xoxb-|lin_api_|secret_|sk-ant-|sk-` returns 0; COO daemon issues test GitHub issue via `/proxy/github/repos/...`; audit_chain row exists
- **SAL-TIR-14** — E2E sovereignty smoke test CI (M, dep TIR-11..13, `hf:openai/gpt-oss-120b:fastest`)
  - APE/V: new workflow asserts (1) smoke_test.sh passes, (2) direct api.github.com from coo fails, (3) proxied call succeeds + audit, (4) unregistered soulkey → 403 P1, (5) audit chain walk verifies all links
- **SAL-TIR-15** — QA review + documentation (S, dep TIR-14, `hf:openai/gpt-oss-120b:fastest`)
  - APE/V: hawkman-qa-a constrained review on all PRs; README + PRINCIPLES.md + runbook lint clean

## 5. Dependency graph

```
TIR-01 ──┬── TIR-02 ── TIR-03 ── TIR-04 ── TIR-05 ── TIR-06
         ├── TIR-07 ───────────────────────────────────┐
         └── TIR-08 (parallel)                          │
                                                        ▼
                                     TIR-09 (compose wire-in)
                                              │
                                     ┌────────┼──────────┐
                                     ▼        ▼          ▼
                                  TIR-10   TIR-12     TIR-13
                                     │        │
                                     └───┬────┘
                                         ▼
                                      TIR-11 (COO refactor, 24h)
                                         │
                                         ▼
                                      TIR-14 (e2e smoke)
                                         │
                                         ▼
                                      TIR-15 (QA + docs)
```

Critical path: ~74h serial; **~48h with 2 parallel implementer slots** (Wave 1 + Wave 3).

## 6. Risk register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | Docker `internal: true` breaks DNS for mc-internal | Med | High | TIR-10 fallback to iptables OUTPUT REJECT in init container; keeps DNS |
| 2 | SDKs (Octokit, Slack client) don't honor custom baseUrl | Med-High | High | TIR-11 starts with "probe each SDK"; swap to raw fetch if any can't redirect |
| 3 | soul-svc MCP plug-in loader (SAL-2552) bypasses tiresias | Low-Med | Med | TIR-11 extends to audit soul-svc MCP client path |
| 4 | Postgres schema collision with soul-svc/portal schemas | Low | Med | Dedicated `tiresias_audit` schema + own role |
| 5 | Cloud model quota mid-epic | Med | Med | Start small (TIR-01, TIR-07) to validate; fall back to hf:openai/gpt-oss-120b for non-critical; reserve qwen3-coder:480b for TIR-11 |

## 7. Cost estimate

- deepseek-v3.2:cloud: free tier likely covers; 0-15 USD paid overflow
- qwen3-coder:480b-cloud: free via OpenRouter; ~1.50 USD paid overflow on TIR-11 (24h)
- hf:openai/gpt-oss-120b:fastest: free

**Total: 0-20 USD ceiling for the epic.** No new infra $.

## 8. Cross-epic touchpoints

### Tiresias exposes
- `GET /healthz` → consumed by Caddy + Fleet-mode readiness gate
- `GET /v1/policies` → `{bundle_sha256, principles[]}` — portal compliance screen
- `GET /v1/audit/recent?limit=N` → audit rows — Epic D central Merkle sync
- `POST /proxy/{service}/{path...}` → **the** programmatic entry point

### New env vars (appliance .env.template)
- `TIRESIAS_MODE=sovereign` (locked)
- `TIRESIAS_DATABASE_URL=postgres://...@postgres:5432/appliance?search_path=tiresias_audit`
- `TIRESIAS_RETENTION_DAYS=365`
- `TIRESIAS_POLICY_MODE=enforce` (override to `log_only` possible)

### Consumer contract
- Every internal service gets `TIRESIAS_URL=http://tiresias-proxy:8840` + `SOULKEY_FILE=/run/secrets/soulkey_<service>`
- Real provider tokens (`GITHUB_TOKEN`, `SLACK_BOT_TOKEN`, `LINEAR_API_KEY`, `NOTION_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) live **only** on `mcp-*` services; holding them elsewhere is an audit finding in the sovereignty smoke

### Other epic dependencies
- **Fleet epic:** needs `/healthz` as readiness gate; uses dedicated `sk_agent_appliance_auditor_<sha>` key for hash-only sync (TIR-12 provisions)
- **Portal epic:** Constitutional Principles viewer reads from `/tiresias/policies` (proxied, unauth)
- **RC tag:** TIR-14 CI green is prerequisite for `v1.0.0-rc.1` tag
