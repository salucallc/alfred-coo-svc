# Budget Alert Rules

This directory contains Alertmanager rule definitions for cost budgeting.

## Overview
- **TenantDailyBudgetExceeded**: Triggers when a tenant's total cost for the current day exceeds the default daily budget of $10.
- **PersonaMonthlyBudgetExceeded**: Triggers when a persona's total cost for the current month exceeds the default monthly budget of $50.

## Testing
A synthetic audit log row with `cost_usd = 15` should generate a Slack alert in the `#batcave` channel within 120 seconds. This is verified by the integration test for ticket OPS-25.

## Deployment
The rules are loaded automatically by Alertmanager when this file is present in the compose configuration. Ensure that Alertmanager is configured with the appropriate Slack webhook (`C0ASAKFTR1C`).

## Maintenance
Adjust thresholds as needed for different customers. Remember to update the Slack webhook URL if it changes.
