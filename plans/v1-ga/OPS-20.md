# OPS-20: 4 provisioned dashboards

## Target paths
- deploy/appliance/grafana/dashboards/appliance_health.json
- deploy/appliance/grafana/dashboards/cost_and_tokens.json
- deploy/appliance/grafana/dashboards/soul_activity.json
- deploy/appliance/grafana/dashboards/auth_and_access.json

## Acceptance criteria
All 4 uids resolve; mc-health green on clean install

## Verification approach
Run `curl http://localhost:3000/api/dashboards/uid/<uid>` for each dashboard and verify HTTP 200 and correct JSON. Check that `mc-health` service reports green status via health endpoint after deployment.

## Risks
- Dashboard JSON may need additional fields for panels; minimal dashboards may lack useful visuals.
- Future changes to Grafana schema could require updates.
