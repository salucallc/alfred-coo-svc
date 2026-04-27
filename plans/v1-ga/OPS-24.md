# OPS-24: Add tiresias-cost-exporter for cost accounting

## Target paths
- `deploy/appliance/tiresias-cost-exporter/main.go`
- `deploy/appliance/tiresias-cost-exporter/Dockerfile`
- `deploy/appliance/tiresias-cost-exporter/go.mod`
- `deploy/appliance/tiresias-cost-exporter/README.md`
- `plans/v1-ga/OPS-24.md`

## Acceptance criteria
- "/metrics returns counters with labels."

## Verification approach
- Run the exporter container in a local compose stack.
- Curl `http://localhost:9090/metrics` and verify the presence of `mc_tokens_total` and `mc_cost_usd_total` lines with the expected label set.
- Unit‑test the `collectMetrics` function against a temporary PostgreSQL fixture.

## Risks
- Database connection credentials must be supplied via environment; missing/incorrect values cause exporter to exit.
- High cardinality of label combinations could increase memory usage in Prometheus.
