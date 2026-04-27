# OPS-24: tiresias-cost-exporter implementation

## Target paths
- deploy/appliance/tiresias-cost-exporter/main.go
- deploy/appliance/tiresias-cost-exporter/Dockerfile
- deploy/appliance/tiresias-cost-exporter/go.mod
- deploy/appliance/tiresias-cost-exporter/README.md
- plans/v1-ga/OPS-24.md

## Acceptance criteria
```
- Implementation matches the plan section for this ticket.
- Unit + integration tests added or updated.
- `ruff` + `pytest` green in CI.
- `propose_pr`; orchestrator will dispatch a hawkman-qa-a review on merge-ready.
- `/metrics` returns counters with labels.
```

## Verification approach
- Build and run the Docker image.
- Curl `http://localhost:9100/metrics` and verify the presence of `mc_tokens_total` and `mc_cost_usd_total` with the expected label set.
- CI runs `go test ./...` (placeholder) and ensures no lint failures.

## Risks
- Go exporter is a new language for the team; risk mitigated by using a simple template and existing Prometheus client library.
