# Fleet Mode Overview

This document describes the fleet mode implementation for Mission Control endpoints. It introduces a field-deployable footprint of `alfred-coo-svc` that operates semi-autonomously while registering with and syncing to a central hub.

## Architecture

The fleet consists of two main components: the Hub and the Endpoints. The Hub resides on an on-prem appliance and manages communication with multiple Endpoints located in various customer networks.

Each Endpoint is essentially the same binary as the standard `alfred-coo-svc`, but configured differently through environment variables and configuration files to operate remotely and report back to the Hub. This configuration is done using the `COO_MODE=endpoint` setting along with mounting a suitable `persona.yaml` file.

### Communication

Communication follows these rules:

- All transport occurs over TLS 1.3 initiated outbound from the endpoint only.
- Persistent WebSocket connection (`/v1/fleet/link`) used primarily, falling back to HTTP long-poll (`/v1/fleet/poll`) where WebSockets are disallowed.
- Authentication starts with a single-use registration token valid for 15 minutes which grants permanent access credentials after successful handshake.
- Identity management handled by issuing unique identifiers for each endpoint upon initial authentication.

### Components

**Hub Additions:**

New additions enable communication with remote devices including:
- Introduction of a dedicated fleet router (`/v1/fleet/*`) built into the soul-svc component.
- Database migrations to facilitate storage requirements related to device tracking and sync events (e.g., SQLite database schema adjustments).
- An additional `fleet-gateway` sidecar to manage incoming/outgoing websocket messages without burdening regular system functions (`soul-svc`).

**Endpoint Footprint:**

Minimal requirements ensure small footprint:
- Runs only required services (`alfred-coo-svc` in endpoint mode, embedded `soul-lite` store) reducing unnecessary dependencies such as PostgreSQL and GUI interfaces found typically in full hubs.
