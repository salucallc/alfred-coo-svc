# OPS-20: Grafana dashboards for Ops

## Target paths
- deploy/appliance/grafana/dashboards/appliance_health.json
- deploy/appliance/grafana/dashboards/cost_and_tokens.json
- deploy/appliance/grafana/dashboards/soul_activity.json
- deploy/appliance/grafana/dashboards/auth_and_access.json

## Acceptance criteria
```
4 pre-provisioned Grafana dashboards:
1. **Appliance Health** (one-pager: services up/down, disk, RAM, CPU, pg connections)
2. **Cost & Tokens**
3. **Soul Activity** (memory writes/s, retrieval p95)
4. **Auth & Access** (login success/fail, who-saw-what audit)
```

## Verification approach
Create the JSON files with appropriate `uid` fields; after deployment Grafana should resolve each dashboard via the API.

## Risks
- Incorrect UID may cause Grafana lookup failures.
- JSON structure must be valid for Grafana import.
