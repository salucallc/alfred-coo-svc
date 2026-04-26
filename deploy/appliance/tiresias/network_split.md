# Network Split Documentation

This document describes the Docker network topology introduced by **SAL-2592 (TIR-10)** to enforce egress isolation.

- **mc-internal**: Internal bridge network with `internal: true`. Services attached cannot reach the external internet. Includes:
  - `alfred-coo-svc`
  - `alfred-portal`
  - `soul-svc`
  - `open-webui`
  - `aletheia-svc`
  - `postgres`
- **mc-egress**: External bridge network without `internal` flag. Services that need outbound internet access attach here, e.g., all MCP gateway services and `caddy`.

The split ensures that the COO daemon (`alfred-coo-svc`) cannot directly resolve external hosts; traffic is routed through the `tiresias-proxy` (not shown) which resides on `mc-internal`.

## Verification
Run the following commands in a test environment:
```sh
docker exec alfred-coo-svc curl --max-time 5 https://api.github.com   # should fail
docker exec mcp-github curl https://api.github.com                 # should succeed
```
These commands confirm the network isolation.

## Risks & Mitigations
- **Mis‑routing**: Incorrect network assignment could break internal service communication. Mitigated by thorough integration tests.
- **DNS failures**: Internal services may lose DNS resolution; a fallback `iptables OUTPUT REJECT` rule is added in the init container as per the ticket notes.
