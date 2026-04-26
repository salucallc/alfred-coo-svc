# OPS-20: Grafana Dashboards

## Target paths
- `deploy/appliance/grafana/dashboards/appliance_health.json`
- `deploy/appliance/grafana/dashboards/cost_and_tokens.json`
- `deploy/appliance/grafana/dashboards/soul_activity.json`
- `deploy/appliance/grafana/dashboards/auth_and_access.json`

## Acceptance criteria
All 4 uids resolve; mc-health green on clean install

## Verification approach
- Deploy the docker-compose stack.
- Query Grafana API `/api/search?type=dash-db` to ensure each UID is present.
- Access the health endpoint to confirm green status.

## Risks
- Dashboard JSON schema mismatch may cause Grafana load errors.
- Missing datasource configuration could break panels.
