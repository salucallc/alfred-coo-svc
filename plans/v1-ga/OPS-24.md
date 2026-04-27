# OPS-24: tiresias-cost-exporter

## Target paths
- deploy/appliance/tiresias-cost-exporter/main.go
- deploy/appliance/tiresias-cost-exporter/Dockerfile
- deploy/appliance/tiresias-cost-exporter/go.mod
- deploy/appliance/tiresias-cost-exporter/README.md
- plans/v1-ga/OPS-24.md

## Acceptance criteria
- Implementation matches the plan section for this ticket.
- Unit + integration tests added or updated.
- `ruff` + `pytest` green in CI.
- PR opened via `propose_pr`; orchestrator will dispatch a hawkman-qa-a review on merge-ready.
- Structured output envelope includes the PR URL in `summary` or `follow_up_tasks`.

## Verification approach
- Build the Docker image and run the container.
- Ensure the `/metrics` endpoint returns `mc_tokens_total` and `mc_cost_usd_total` gauges with the expected label set.
- Verify the exporter updates values at 60‑second intervals.

## Risks
- Minimal risk: exporter is read‑only and does not modify existing services.
