# OPS-20: Grafana Dashboards

## Target paths
- deploy/appliance/grafana/dashboards/appliance_health.json
- deploy/appliance/grafana/dashboards/cost_and_tokens.json
- deploy/appliance/grafana/dashboards/soul_activity.json
- deploy/appliance/grafana/dashboards/auth_and_access.json

## Acceptance criteria
- All 4 uids resolve; mc-health green on clean install.

## Verification approach
- Deploy the appliance using `docker compose up`.
- Access Grafana at `/ops/` and verify the four dashboards load without errors.
- Run the health check script `./mc.sh health` and ensure it reports green.

## Risks
- Dashboard JSON schema mismatches Grafana version (unlikely as using OSS 11.4.0).
- Large dashboard files could increase image size; these are minimal.
