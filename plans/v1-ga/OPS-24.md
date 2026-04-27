# OPS-24: tiresias-cost-exporter

## Target paths
- deploy/appliance/tiresias-cost-exporter/main.go
- deploy/appliance/tiresias-cost-exporter/Dockerfile
- deploy/appliance/tiresias-cost-exporter/go.mod
- deploy/appliance/tiresias-cost-exporter/README.md
- plans/v1-ga/OPS-24.md

## Acceptance criteria
- /metrics returns counters with labels.

## Verification approach
Run the exporter, curl http://localhost:8080/metrics and verify the presence of `mc_tokens_total` and `mc_cost_usd_total` with labelled dimensions.

## Risks
- None beyond standard Go binary size; ensure the exporter does not consume excessive CPU.
