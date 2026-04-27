# OPS-24: Add tiresias-cost-exporter for cost accounting

## Target paths
- deploy/appliance/tiresias-cost-exporter/main.go
- deploy/appliance/tiresias-cost-exporter/Dockerfile
- deploy/appliance/tiresias-cost-exporter/go.mod
- deploy/appliance/tiresias-cost-exporter/README.md
- plans/v1-ga/OPS-24.md

## Acceptance criteria
- [ ] Address every point in the review feedback below.
- [ ] Tests still green (`ruff` + `pytest`).
- [ ] Push fixes to the EXISTING branch for https://github.com/salucallc/alfred-coo-svc/pull/146 via the `update_pr` tool; do NOT open a new PR. The reviewer bot will re-review automatically once your new commit lands.

## Verification approach
- Build the Go binary inside the Docker image and run the container.
- Query `http://localhost:8080/metrics` and verify that `mc_tokens_total` and `mc_cost_usd_total` appear with the expected label sets.
- Run existing `ruff` and `pytest` suites to ensure they remain green.

## Risks
- The exporter currently contains a stub implementation; real audit‑log querying must be added before production use.
- Adding a new service may increase resource consumption; monitor CPU/memory after deployment.
- Ensure the port (8080) does not conflict with existing services in the compose network.
