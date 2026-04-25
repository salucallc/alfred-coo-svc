# OPS-16: OTEL collector + receivers

## Target paths
- deploy/appliance/docker-compose.yml
- deploy/appliance/otel/otel-collector-config.yaml
- deploy/appliance/otel/README.md

## Acceptance criteria
- `curl otel-collector:4318/v1/traces -d '{}'` → 200

## Verification approach
- Ensure `docker compose config` succeeds.
- Deploy the stack and execute the curl command against the collector; verify HTTP 200.
- Check that Prometheus and Loki receive metrics/logs.

## Risks
- Collector configuration may need tuning for production load.
- Network routing between services must allow access to ports 4317/4318.
