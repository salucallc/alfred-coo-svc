# OPS-20: 4 pre-provisioned Grafana dashboards

## Target paths
- deploy/appliance/grafana/dashboards/appliance_health.json
- deploy/appliance/grafana/dashboards/cost_and_tokens.json
- deploy/appliance/grafana/dashboards/soul_activity.json
- deploy/appliance/grafana/dashboards/auth_and_access.json

## Acceptance criteria
- All 4 uids resolve via /api/dashboards/uid/<id>; mc-health green on clean install.

## Verification approach
- Add the four JSON dashboard files under `deploy/appliance/grafana/dashboards/`.
- Run `docker compose up -d` for the appliance.
- Verify Grafana loads each dashboard without errors.
- Curl each dashboard UID endpoint (`/api/dashboards/uid/<uid>`) returns HTTP 200.
- Query the health endpoint (`/health` or `mc-health`) returns green status.

## Risks
- Incorrect UID values would cause dashboard load failures.
- Grafana version mismatch could break JSON schema.
- Deployment ordering: Grafana must be ready before dashboards are provisioned.
