# tiresias-cost-exporter

A lightweight Prometheus exporter that scrapes the `tiresias_audit_log` every 60 seconds and emits two gauge metrics:

- `mc_tokens_total{tenant_id,persona_id,model,provider}`
- `mc_cost_usd_total{tenant_id,persona_id,model,provider}`

It is used for the OPS‑24 ticket to provide cost and token accounting metrics at `/metrics` on port 8080.
