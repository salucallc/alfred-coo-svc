# tiresias-cost-exporter

A lightweight Go exporter that scrapes the `tiresias_audit_log` every 60 seconds and emits Prometheus metrics:

- `mc_tokens_total{tenant_id,persona_id,model,provider}` – total tokens used.
- `mc_cost_usd_total{tenant_id,persona_id,model,provider}` – total cost in USD.

The exporter listens on port **8080** and exposes the `/metrics` endpoint for Prometheus to scrape.

## Build & Run
```sh
cd deploy/appliance/tiresias-cost-exporter
docker build -t tiresias-cost-exporter .
docker run -p 8080:8080 tiresias-cost-exporter
```
