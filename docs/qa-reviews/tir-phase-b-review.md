## hawkman-qa-a (hf:openai/gpt-oss-120b:fastest) Phase‑B Aggregate Report

**Model:** `hf:openai/gpt-oss-120b:fastest`  
**Run Date:** 2026-04-28  
**Scope:** TIR-01..TIR-15 PRs in tiresias epic

| PR | Title | Verdict | Rationale (≤300 chars) |
|---|---|---|---|
| [#108] | [TIR-01] scaffold tiresias-sovereign repo + CI image | **PASS** | Repo scaffold, CI, healthz all present; small scope; no logic bugs. CI badge green. Image builds on push. Standard go-repos layout. Risk: low. |
| [#109] | [TIR-02] Embed principle_registry.json + hash-chain loader | **PASS** | 12 principles loaded correctly; hash-chain validated; /v1/policies returns bundle_sha256. Test suite covers all 12 rules. Boundary: none. |
| [#110] | [TIR-07] DB migrations for tiresias_audit schema | **PASS** | Migration creates schema + 4 tables; golang-migrate up/down idempotent; embedded migration runs on boot. SQL reviewed; index coverage ok. |
| [#111] | [TIR-08] Add mcp-llm cascade router | **PASS** | Port 8220 responds; anthropic→openai→ollama cascade works; smoke curl returns 200. Config validated. Timeout handling present. Coverage: good. |
| [#112] | [TIR-03] Soulkey auth middleware (identity P1-P3) | **PASS** | Missing/malformed → 401; unregistered → 403; valid → 200. Table-driven tests cover all branches. Middleware ordering correct. Security: tight. |
| [#113] | [TIR-04] Proxy handler + destination allowlist | **PASS** | Allowlist forwards or 403s correctly; /proxy/mock/ping returns pong. Config parsing tested. Boundary enforcement validated. No bypass paths. |
| [#114] | [TIR-05] Audit hash-chain writer (accountability P7-P9) | **PASS** | Every request writes audit row; 100 sequential requests → chain integrity walk passes. Hash linking correct. Retention config present. |
| [#115] | [TIR-06] Transparency headers (P10-P12) | **PASS** | X-Tiresias-Principles-Passed, Policy-Bundle, Audit-ID on all responses; Deny-Reason on denies. Headers tested in integration. Format correct. |
| [#116] | [TIR-09] Wire into appliance compose | **PASS** | /tiresias/healthz and /tiresias/policies accessible via Caddy; policies returns .principles | length == 12. Network routing correct. Compose mounts ok. |
| [#117] | [TIR-10] Split docker networks | **PASS** | mc-internal no egress; mc-egress mcp-only. Verified via docker exec curl tests. DNS isolation works. iptables fallback documented. |
| [#118] | [TIR-12] mc-init mints soulkeys + registers allowlist | **PASS** | 6 soulkeys generated idempotently; _soulkeys rowcount=6; _soulkey_allowlist for coo has ≥4 rows. Init script idempotent. Key format matches spec. |
| [#119] | [TIR-13] Update open-webui to route through tiresias | **PASS** | Chat completion via browser works; audit_chain row increments per turn. WebUI config updated. No direct LLM calls remain. E2E smoke passes. |
| [#120] | [TIR-11] Remove raw tokens from coo-svc; wire SOULKEY + TIRESIAS_URL | **CONCERN** | Tokens removed; COO routes via /proxy/github. However, fallback to direct call exists in error path. Risk: bypass possible under error conditions. |
| [#121] | [TIR-14] E2E sovereignty smoke test CI | **CONCERN** | Smoke test passes, yet direct-egress iptables enforcement absent from CI; risk of regression. Test doesn't assert network isolation. Coverage gap. |
| [#122] | [TIR-15] QA review + documentation | **PASS** | README + PRINCIPLES.md present and lint clean. All PRs reviewed. Runbook covers operational procedures. Docs complete and accurate. |

**Summary:** 13 PASS, 2 CONCERN (TIR-11, TIR-14). No FAIL. Review coverage: 100%.