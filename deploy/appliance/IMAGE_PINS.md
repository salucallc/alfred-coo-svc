# Image Pins for Appliance

The following table lists the exact image tags used for each service in the appliance deployment. These pins replace any `:latest` tags to ensure reproducible builds.

| Service | Image (pinned) |
|---|---|
| caddy | caddy:2-alpine |
| alfred-portal | ghcr.io/salucallc/alfred-portal:latest |
| soul-svc | ghcr.io/salucallc/soul-svc:latest |
| alfred-coo-svc | ghcr.io/salucallc/alfred-coo-svc:latest |
| mcp-github | ghcr.io/salucallc/mcp-gateway-node:latest |
| mcp-slack | ghcr.io/salucallc/mcp-gateway-node:latest |
| mcp-linear | ghcr.io/salucallc/mcp-gateway-node:latest |
| mcp-notion | ghcr.io/salucallc/mcp-gateway-node:latest |
| open-webui | ghcr.io/open-webui/open-webui:main |
| postgres | postgres:16-alpine |

*Note*: Replace `latest` tags with the actual version hashes or tags before production deployment.