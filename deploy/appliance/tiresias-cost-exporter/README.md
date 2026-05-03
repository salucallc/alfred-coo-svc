# tiresias-cost-exporter

A lightweight Go exporter that scrapes the `tiresias_audit_log` every 60 seconds and exposes Prometheus metrics:

- `mc_tokens_total{tenant_id, persona_id, model, provider}`
- `mc_cost_usd_total{tenant_id, persona_id, model, provider}`

The service listens on port **8080** and serves metrics at `/metrics`.
