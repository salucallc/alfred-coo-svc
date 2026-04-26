# OPS-20: 4 provisioned Grafana dashboards

## Target paths
- deploy/appliance/grafana/dashboards/appliance_health.json
- deploy/appliance/grafana/dashboards/cost_and_tokens.json
- deploy/appliance/grafana/dashboards/soul_activity.json
- deploy/appliance/grafana/dashboards/auth_and_access.json
- plans/v1-ga/OPS-20.md

## Acceptance criteria
- All 4 uids resolve; mc-health green on clean install

## Verification approach
Deploy the compose stack and query Grafana API `/api/search?type=dash-db` to confirm the four dashboard UIDs exist and the health panel reports green on a clean install. Automated test in `tests/test_grafana_dashboards.py` asserts HTTP 200 and correct UID presence.

## Risks
- Future Grafana schema changes may require dashboard updates.
- Caddy routing misconfiguration could block Grafana access.
