# OPS-25: Budget alert rules

## Target paths
- deploy/appliance/alertmanager/budget_rules.yml
- deploy/appliance/alertmanager/budget_rules_README.md
- plans/v1-ga/OPS-25.md

## Acceptance criteria
- $15 test row in tiresias_audit_log → #batcave alert within 120s

## Verification approach
- Insert a synthetic audit log row with `cost_usd = 15` using the test harness.
- Verify that Alertmanager forwards a Slack message to `#batcave` within 120 seconds.
- Run `docker compose up` and ensure the new files are mounted and Alertmanager picks up the rules.

## Risks
- Alert thresholds may need tuning for real customers.
- Slack webhook misconfiguration could hide alerts.
- Deleting existing alert rules could cause unintended alerts (guarded by deletion limits).
