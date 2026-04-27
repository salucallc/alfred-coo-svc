# Tiresias Cost Exporter

A lightweight Prometheus exporter written in Go that scrapes the `tiresias_audit_log` (placeholder implementation) every 60 seconds and exposes two gauges:

- `mc_tokens_total{tenant_id, persona_id, model, provider}` – total tokens processed.
- `mc_cost_usd_total{tenant_id, persona_id, model, provider}` – total cost in USD.

## Building

```sh
cd deploy/appliance/tiresias-cost-exporter
docker build -t tiresias-cost-exporter:latest .
```

## Running locally

```sh
docker run -p 8080:8080 tiresias-cost-exporter:latest
```

The metrics are available at `http://localhost:8080/metrics`.
