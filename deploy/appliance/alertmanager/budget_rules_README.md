# Budget Alert Rules

This file defines Prometheus alerting rules for per‑tenant and per‑persona budget monitoring.

- **BudgetAlert** triggers when the aggregated `mc_cost_usd_total` metric exceeds $15.
- The alert is configured to fire after 1 minute of sustained breach and sends a notification to the Slack channel `#batcave` via Alertmanager's webhook.
- Adjust the threshold or `for` duration as needed for production environments.
