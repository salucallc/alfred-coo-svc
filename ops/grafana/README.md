# Grafana dashboards

Dashboard JSON for the Loki-backed alfred-grafana instance running on
Oracle (`100.105.27.63:3300`).

## Dashboards in this directory

| File | UID | Purpose |
|---|---|---|
| `doctor_metrics_dashboard.json` | `alfred-coo-doctor-metrics` | Phase 3a: per-tick doctor scan duration, failure-class counters, baseline soak progress, raw tick log stream. |

## Import / update

The dashboards are versioned in this repo so the source of truth for
panel layout + LogQL queries is auditable. To push a new version into
the live Grafana instance:

```bash
DASHBOARD=ops/grafana/doctor_metrics_dashboard.json

# Wrap the dashboard JSON inside the API envelope.
jq '{dashboard: ., overwrite: true, message: "ops/grafana sync"}' "$DASHBOARD" \
  | curl -s -X POST -u admin:"$GRAFANA_ADMIN_PASSWORD" \
      -H "Content-Type: application/json" \
      --data @- \
      http://100.105.27.63:3300/api/dashboards/db
```

`GRAFANA_ADMIN_PASSWORD` is the value of `GF_SECURITY_ADMIN_PASSWORD`
on the Oracle docker host.

## Why JSON over UI-only edits

Grafana lets operators edit dashboards in the UI and persist directly
to its embedded sqlite. That works for one-off tweaks but loses change
history, makes panel-query review impossible in PRs, and blocks the
"appliance container" story (anyone deploying the container needs the
dashboards to come up out of the box).

When the dashboards stabilise, we move them into Grafana's filesystem
provisioning path (`/var/lib/grafana/dashboards/`) so they auto-load
on container start.
