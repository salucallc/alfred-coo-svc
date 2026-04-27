# Budget Alert Rules

These Prometheus Alertmanager rules enforce per‑tenant daily and per‑persona monthly cost budgets. When the total USD cost for a tenant exceeds **$10** in a day, or a persona exceeds **$50** in a month, an alert is fired and sent to the `#batcave` Slack channel via the configured webhook.

## How it works
- `mc_cost_usd_total` is a gauge exported by the `tiresias-cost-exporter`.
- The first rule aggregates costs by `tenant_id` and checks against the **$10** daily threshold.
- The second rule aggregates costs by `tenant_id` and `persona_id` and checks against the **$50** monthly threshold.
- Alerts fire after 2 minutes of sustained breach (`for: 2m`) to avoid flapping.

## Deployment
The file is mounted into the Alertmanager container via the `docker-compose.yml` configuration. Alertmanager automatically reloads the configuration on container restart.

## Testing
A synthetic audit‑log entry of **$15** cost is inserted in tests to verify that the `TenantDailyBudgetExceeded` alert is emitted and the Slack webhook posts a message within 120 seconds.
