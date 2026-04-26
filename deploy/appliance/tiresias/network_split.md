# Network Split for Tiresias Integration (TIR-10)

This document describes the split Docker networks introduced in **TIR-10**:

- **mc-internal** – internal bridge used by services that must **not** have direct internet egress (COO daemon, portal, open‑webui, soul‑svc). All traffic from these services must flow through the `tiresias-proxy`.
- **mc-egress** – bridge for edge services (Caddy, all MCP gateways, `aletheia-svc`) that are allowed to reach external APIs. These services connect to the internet via the `mc-egress` bridge.

The split ensures that the COO daemon cannot bypass the policy proxy, satisfying the APE/V condition that a direct `curl` to `https://api.github.com` fails, while the `mcp-github` service can still reach the same endpoint.
