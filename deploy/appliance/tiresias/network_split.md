# Network Split Overview

This document describes the Docker network segmentation introduced in TIR-10.

- **mc-internal**: Bridge network for internal services (`alfred-coo`, `portal`, `open-webui`, `soul`). No internet egress.
- **mc-egress**: Bridge network for egress services (`caddy`, `mcp-github`, `mcp-slack`, `mcp-linear`, `mcp-notion`, `mcp-llm`). Allows outbound traffic to external APIs.

The `docker-compose.yml` is updated to attach services to the appropriate network. See the compose file for exact network names.
