# Network Split for Tireisias Integration

This document describes the Docker network topology introduced in SAL-2592 (TIR-10) to enforce internal isolation:

- **mc-internal**: bridge network with `internal: true`. Services attached here cannot reach the internet. It hosts `alfred-coo-svc` and other internal components.
- **mc-egress**: regular bridge network used for outbound egress through MCP services (e.g., `mcp-github`, `mcp-slack`). Services needing external access attach to this network.

The `alfred-coo-svc` service is connected to both networks to allow internal communication while routing external calls via `tiresias-proxy` on `mc-egress`.
