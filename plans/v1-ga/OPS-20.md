# OPS-20: Add Grafana dashboards

## Target paths
- deploy/appliance/grafana/dashboards/appliance_health.json
- deploy/appliance/grafana/dashboards/cost_and_tokens.json
- deploy/appliance/grafana/dashboards/soul_activity.json
- deploy/appliance/grafana/dashboards/auth_and_access.json

## Acceptance criteria
- All 4 uids resolve; mc-health green on clean install.

## Verification approach
- Deploy the appliance and ensure Grafana loads each dashboard via its UID.
- Verify the health check service reports `mc-health` as green.

## Risks
- Dashboard JSON syntax errors could prevent loading.
- UID collisions with existing dashboards.
