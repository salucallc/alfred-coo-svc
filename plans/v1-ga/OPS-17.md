# OPS-17: Prometheus + scrape config

## Target paths
- deploy/appliance/prometheus/prometheus.yml
- deploy/appliance/prometheus/scrape_configs.yml

## Acceptance criteria
- `/api/v1/targets` shows ≥5 `up=1` targets

## Verification approach
- Deploy the new Prometheus container configuration.
- Query `http://localhost:9090/api/v1/targets` and verify at least five targets have `up=1`.

## Risks
- Services may not expose metrics on port 9090, causing targets to be down.
- Network DNS resolution issues within the `mc-ops` network.
