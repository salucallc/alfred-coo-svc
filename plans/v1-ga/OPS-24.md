# OPS-24: Cost + Token Accounting Exporter

## Target paths
- deploy/appliance/tiresias-cost-exporter/main.go
- deploy/appliance/tiresias-cost-exporter/Dockerfile
- deploy/appliance/tiresias-cost-exporter/go.mod
- deploy/appliance/tiresias-cost-exporter/README.md

## Acceptance criteria
- `/metrics` returns `mc_tokens_total{tenant_id,persona_id,model,provider}` and `mc_cost_usd_total{tenant_id,persona_id,model,provider}` counters with appropriate label values.
- Exporter runs as a Docker container exposing port 8080.
- No existing files in the repository are deleted beyond the allowed limits.

## Verification approach
- Deploy the container locally and curl `http://localhost:8080/metrics`.
- Verify the presence of both metric families and that they contain sample label sets.

## Risks
- Potential schema changes in `tiresias_audit_log` breaking the scrape logic.
- Resource usage of the exporter on low‑end appliances (minimal Go binary, low CPU).
