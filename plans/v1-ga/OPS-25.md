# OPS-25: Budget Alert Rules

## Target paths
- deploy/appliance/alertmanager/budget_rules.yml
- deploy/appliance/alertmanager/budget_rules_README.md
- plans/v1-ga/OPS-25.md

## Acceptance criteria
- Implementation matches the plan section for this ticket.
- Unit + integration tests added or updated.
- `ruff` + `pytest` green in CI.
- Synthetic $15 test row in tiresias_audit_log → #batcave alert within 120s.

## Verification approach
- Add alert rules to Alertmanager configuration.
- Deploy via `docker-compose` and ensure Alertmanager picks up the new rules.
- Insert a synthetic audit‑log row (cost $15) via the `tiresias-cost-exporter` test endpoint or direct DB insertion.
- Verify the Slack webhook receives a message within 120 seconds (mocked in CI).
- Run CI checks: linter (`ruff`) and tests (`pytest`).

## Risks
- Misconfiguration of alert thresholds may cause false positives or missed alerts.
- Exposure of the Slack webhook URL – ensure it is stored securely.
- Alertmanager reload failure if YAML syntax is incorrect.
