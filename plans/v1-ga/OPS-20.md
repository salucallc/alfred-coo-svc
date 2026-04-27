# OPS-20: Add Grafana dashboards

## Target paths
- deploy/appliance/grafana/dashboards/appliance_health.json
- deploy/appliance/grafana/dashboards/cost_and_tokens.json
- deploy/appliance/grafana/dashboards/soul_activity.json
- deploy/appliance/grafana/dashboards/auth_and_access.json

## Acceptance criteria
- All 4 uids resolve; mc-health green on clean install

## Verification approach
- Deploy with `docker compose up`; Grafana loads the four dashboards; health endpoint reports green.

## Risks
- None significant; merely adding static JSON files.
