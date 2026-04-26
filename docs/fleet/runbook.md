# Fleet Endpoint Runbook

## Provisioning
1. Deploy `alfred-coo-svc` in endpoint mode with `COO_MODE=endpoint`.
2. Provide a one‑time registration token via environment variable.
3. Run the registration command; record the returned `endpoint_id` and `api_key`.

## Key Rotation
- Keys rotate automatically every 24h; watch for `api_key_rotation` in heartbeat acks.

## Quarantine Recovery
- If the endpoint enters quarantine, use `mcctl endpoint unquarantine` after fixing the underlying issue.
