# OPS-20: 4 provisioned dashboards

## Target paths
- deploy/appliance/grafana/dashboards/appliance_health.json
- deploy/appliance/grafana/dashboards/cost_and_tokens.json
- deploy/appliance/grafana/dashboards/soul_activity.json
- deploy/appliance/grafana/dashboards/auth_and_access.json

## Acceptance criteria
- [ ] Implementation matches the plan section for this ticket.
- [ ] Unit + integration tests added or updated.
- [ ] `ruff` + `pytest` green in CI.
- [ ] PR opened via `propose_pr`; orchestrator will dispatch a hawkman-qa-a review on merge-ready.
- [ ] Structured output envelope includes the PR URL in `summary` or `follow_up_tasks`.

## Verification approach
- Add the dashboard JSON files.
- Verify each dashboard is accessible via Grafana API: `curl -s http://grafana:3000/api/dashboards/uid/<uid>` returns HTTP 200.
- Run unit and integration tests; ensure CI passes.

## Risks
- UID collisions if dashboards already exist.
- Grafana service must be running for verification.
