Here's the full `07_PUBLIC_README.md` following all constraints:

```markdown
# Mission Control
Persistent, persona-aware COO daemon for autonomous mesh-task execution.

[![License](https://img.shields.io/badge/license-PolyForm--Noncommercial--1.0.0-blue)](https://polyformproject.org/licenses/noncommercial/1.0.0/)
[![Build Status](https://img.shields.io/github/actions/workflow/status/salucallc/mission-control/ci.yml?branch=main)](https://github.com/salucallc/mission-control/actions)
[![Version](https://img.shields.io/github/v/release/salucallc/mission-control)](https://github.com/salucallc/mission-control/releases)

## What it is

Mission Control is a headless service that manages autonomous task execution across distributed AI personas. It polls a task mesh, routes tasks to appropriate model tiers based on persona profiles, dispatches to cloud or local inference endpoints, validates outputs for consistency and drift, and writes results back to shared memory. The daemon runs as a systemd service or in Docker, with structured logging and health endpoints for observability.

Unlike generic agent runners, Mission Control enforces persona discipline through Cedar policies, implements governance-as-git for operator procedures, and provides hand-back triggers that escalate tasks to human operators rather than failing silently. Each task tag maps to a specific model tier and system prompt, with cost caps and rate limits per persona. The system includes a plugin architecture for declarative extension of personas, tools, workflows, and model adapters.

The default deployment pairs with a task mesh service (soul-svc or compatible), a memory service (soul-memory or compatible), and any MCP-compliant tool server. The reference stack launches with a single `docker compose up` command, though Kubernetes and bare-metal deployments are supported through documented configuration paths.

## Why it exists

Agent frameworks excel at single-shot reasoning but often fail in long-running, unattended operation. Tasks accumulate, personas drift from their intended behavior, costs escalate unpredictably, and sensitive data leaks into logs. Mission Control addresses these operational concerns with built-in governance: persona discipline through policy enforcement, cost controls, escalation triggers, and sovereign execution boundaries.

The system is designed for deployments where AI agents must operate autonomously for extended periods without human supervision. It provides the operational layer that maintains consistency, controls costs, and ensures safe escalation paths when agents encounter edge cases beyond their design parameters.

## Install

The fastest path to evaluation is Docker Compose. For Kubernetes or bare-metal deployments, see `INSTALL.md`.

```bash
git clone https://github.com/salucallc/mission-control.git
cd mission-control
cp .env.sample .env   # fill in SOUL_API_KEY and model provider keys
docker compose up -d
```

## 30-second tour

1. The health endpoint at `/health` returns 200 OK within seconds of startup.
2. The daemon heartbeat appears in the task mesh within one minute.
3. A synthetic test task is claimed and completed automatically, visible in logs.
4. Logs are structured JSON with consistent fields for filtering and analysis.
5. The optional web UI renders agent tiles showing persona status and recent activity.

## Plugin extensibility

Mission Control includes a declarative plugin system with six extension points: personas, MCP tools, workflows, connectors, policies, and model adapters. Plugins are hot-reloadable, Cedar-sandboxed, and version-pinned with SemVer. To add a plugin, drop a file into `/opt/mission-control/plugins/<type>/<id>/` and send SIGHUP to the daemon. The full plugin contract is documented in `docs/plugin-architecture.md`.

## License summary

Mission Control is dual-licensed. Source code is available under PolyForm-Noncommercial-1.0.0, which permits personal, research, evaluation, and other noncommercial use. Commercial use, including production deployments and revenue-generating applications, requires a separate commercial license.

Commercial licenses are available from Saluca LLC in four tiers: Hobbyist (free for individual noncommercial use, same as PolyForm default), Team (self-serve via Stripe checkout for small production deployments), Enterprise (for multi-tenant and regulated environments), and Custom/OEM (for redistribution rights). Contact `info@saluca.com` for Enterprise and OEM inquiries; self-serve tiers are available at `https://saluca.com/mission-control/pricing`.

```
License: PolyForm-Noncommercial-1.0.0 (source) + commercial track (Saluca LLC)
Commercial: info@saluca.com | https://saluca.com/mission-control/pricing
```

## Contributing

Contributions are welcome via pull request. All changes to operator procedures, personas, and plugins follow a governance-as-git model: proposed changes are submitted as PRs, validated by CI (schema checks, Cedar policy compilation, persona-drift tests), and merged by maintainers. External contributors must sign a CLA, available in `CLA.md`. Telemetry is disabled by default; contributors may enable it locally for debugging purposes.

Quick contribution paths:
- Add a new persona: drop a markdown file in `plugins/personas/`, open a PR
- Add a new MCP tool: drop a YAML manifest in `plugins/tools/`, open a PR
- Fix a bug or add a feature: branch from main, submit a PR, pass CI, request review

## Links to in-depth docs

- [`docs/architecture.md`](docs/architecture.md)
- [`docs/plugin-architecture.md`](docs/plugin-architecture.md)
- [`docs/deploy.md`](docs/deploy.md)
- [`docs/license.md`](docs/license.md)
- [`docs/governance.md`](docs/governance.md)
- [`docs/security.md`](docs/security.md)

The project is maintained by Saluca LLC and contributors. Community discussion is available at [https://saluca.com/community](https://saluca.com/community).
```
