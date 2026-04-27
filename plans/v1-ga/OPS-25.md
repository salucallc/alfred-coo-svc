# OPS-25: Budget alert rules

## Target paths
- deploy/appliance/alertmanager/budget_rules.yml
- deploy/appliance/alertmanager/budget_rules_README.md
- plans/v1-ga/OPS-25.md

## Acceptance criteria
```
$15 test row → #batcave alert within 120s
```

## Verification approach
- Insert a synthetic audit log row with `cost_usd = 15.01`.
- Verify Alertmanager forwards a Slack message to `#batcave` within 120 seconds.
- Confirm the Prometheus rule `BudgetAlert` fires as expected.

## Risks
- False positives if cost aggregation miscalculates.
- Slack webhook misconfiguration could suppress alerts.
- Metric `mc_cost_usd_total` must be reliably exported by `tiresias-cost-exporter`.
