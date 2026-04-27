# Tiresias Cost Exporter

A small Go exporter that scrapes the `tiresias_audit_log` table every 60 seconds and exposes two Prometheus counters:

- `mc_tokens_total{tenant_id,persona_id,model,provider}` – total tokens processed.
- `mc_cost_usd_total{tenant_id,persona_id,model,provider}` – total cost in USD.

The exporter runs as a Docker container on the appliance and is reachable at `http://tiresias-cost-exporter:9090/metrics`.

## Building
```sh
docker build -t tiresias-cost-exporter ./deploy/appliance/tiresias-cost-exporter
```

## Running (in docker‑compose)
Add a service entry referencing the built image and expose port 9090.
