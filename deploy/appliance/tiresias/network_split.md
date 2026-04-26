# Network Split Documentation

This document describes the two Docker networks introduced for the appliance:

- **`mc-internal`** – a bridge network marked `internal: true`. Services attached to this network have **no external internet access**. It includes:
  - `alfred-coo-svc`
  - `alfred-portal`
  - `soul-svc`
  - `open-webui`
  - `postgres`
  - `aletheia-svc`

- **`mc-egress`** – a standard bridge network with internet access. It is used by services that need to reach external APIs via the Tiresias proxy, such as the MCP gateway services:
  - `caddy`
  - `mcp-github`
  - `mcp-slack`
  - `mcp-linear`
  - `mcp-notion`

The configuration ensures that the COO daemon (`alfred-coo-svc`) cannot directly reach `api.github.com`; instead it must go through the `tiresias-proxy` on the `mc-egress` network.
