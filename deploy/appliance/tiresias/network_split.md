# Network Split Documentation

This document describes the split Docker network topology for the appliance.

- `mc-internal`: internal bridge network with no internet egress for core services.
- `mc-egress`: bridge network allowing egress only for MCP services and Caddy.

The COO, portal, and open-webui services are attached to `mc-internal` and must route external requests via the `tiresias-proxy`.
