# tiresias-cost-exporter

A lightweight Prometheus exporter written in Go that scrapes the `tiresias_audit_log` (not implemented in this stub) and exposes two metrics:

- `mc_tokens_total{tenant_id, persona_id, model, provider}` – total token count.
- `mc_cost_usd_total{tenant_id, persona_id, model, provider}` – total cost in USD.

The service listens on port **9100** and serves metrics at `/metrics`. It is intended to run as part of the `alfred-coo-svc` appliance compose.
