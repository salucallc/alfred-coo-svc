# OPS-20: Add pre-provisioned Grafana dashboards

## Target paths
- deploy/appliance/grafana/dashboards/appliance_health.json
- deploy/appliance/grafana/dashboards/cost_and_tokens.json
- deploy/appliance/grafana/dashboards/soul_activity.json
- deploy/appliance/grafana/dashboards/auth_and_access.json

## Acceptance criteria
- All 4 uids resolve; mc-health green on clean install

## Verification approach
- Deploy with `docker compose up`; verify Grafana dashboards are available and each loads without errors; ensure health status is green.

## Risks
- Dashboard JSON syntax errors causing Grafana import failures.
- Inconsistent UID references if Grafana provisioning changes.
