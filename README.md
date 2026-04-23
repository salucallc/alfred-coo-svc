# alfred-coo-svc

Headless COO daemon that claims tasks from the Saluca mesh, routes them by persona to Ollama Cloud models, runs a multi-turn OpenAI-compatible tool-use loop for tool-enabled personas, and writes back structured envelopes to `/v1/mesh/tasks/{id}/complete`.

## Architecture

- `src/alfred_coo/main.py` — poll → claim → dispatch → complete loop; loads persona-scoped soul memory as context; persists artifact paths + tool-call log on completion.
- `src/alfred_coo/persona.py` — `BUILTIN_PERSONAS` registry; each entry bundles system prompt, preferred + fallback model, topic scope, and opt-in tool list.
- `src/alfred_coo/dispatch.py` — model-agnostic caller; `select_model` resolves `[tag:code]` / `[tag:strategy]` overrides; `call_with_tools` runs the multi-turn tool loop (max 8 iterations) against Ollama or OpenRouter OpenAI-compatible endpoints.
- `src/alfred_coo/tools.py` — five built-in tools (`linear_create_issue`, `slack_post`, `mesh_task_create`, `propose_pr`, `http_get`); each is an async handler with a JSON schema rendered to OpenAI function form.
- `src/alfred_coo/artifacts.py` — path-safe artifact writer: structured envelope entries land on disk under the task workspace with `..`-escape rejected.

## Tool inventory

| Name | Purpose | Required env | Notes |
|---|---|---|---|
| `linear_create_issue` | Open issue in Saluca SAL team | `LINEAR_API_KEY` or `ALFRED_OPS_LINEAR_API_KEY` | Priority 0-4 (default 3); optional `due_date` (YYYY-MM-DD). |
| `slack_post` | Post message to Slack | `SLACK_BOT_TOKEN` or `SLACK_BOT_TOKEN_ALFRED` | Defaults to `#batcave` (`C0ASAKFTR1C`); override via `SLACK_BATCAVE_CHANNEL` or per-call `channel`. |
| `mesh_task_create` | Queue new mesh task for another persona | `SOUL_API_KEY` | Prepends `[persona:<name>]` + free-form tags to the title so the daemon parser routes on claim. |
| `propose_pr` | Atomic clone → branch → commit → push → open PR | `GITHUB_TOKEN` | Org allowlist: `salucallc`, `saluca-labs`, `cristianxruvalcaba-coder`. Uses GitHub REST API directly (no `gh` CLI dependency). Workspaces keyed by task_id via ContextVar. |
| `http_get` | Allowlisted read-only GET | none | 256 KB cap; text/json/xml/yaml only; hosts: Saluca GitHub paths, `*.saluca.com`, `*.tiresias.network`, `*.asphodel.ai`, arxiv, canonical docs. |

## Personas

| Name | Preferred | Fallback | Tools | Topics |
|---|---|---|---|---|
| `default` | `deepseek-v3.2:cloud` | `qwen3-coder:480b-cloud` | (none) | (none) |
| `alfred-coo-a` | `qwen3-coder:480b-cloud` | `deepseek-v3.2:cloud` | all five | coo-daemon, unified-plan, gap-closure, mission-control, autonomous-ops |
| `mr-terrific-a` | `qwen3-coder:480b-cloud` | `deepseek-v3.2:cloud` | (none) | pq, sovereign-pq, security, karolin-sovereign-pq, crypto |
| `innovation-pm` | `deepseek-v3.2:cloud` | `qwen3-coder:480b-cloud` | (none) | twin-rho, mnemosyne, hypnos, ahi, innovation, research |
| `revenue-pm` | `deepseek-v3.2:cloud` | `qwen3-coder:480b-cloud` | (none) | stripe, pricing, onboarding, revenue, funnel, billing |
| `ventures-pm` | `deepseek-v3.2:cloud` | `qwen3-coder:480b-cloud` | (none) | impulse, ventures, arb-bot, trading |
| `investment-pm` | `deepseek-v3.2:cloud` | `qwen3-coder:480b-cloud` | (none) | patent, fundraising, investor, investment, ip |
| `operations-pm` | `deepseek-v3.2:cloud` | `qwen3-coder:480b-cloud` | (none) | audit, pipeline, operations, deploy, runbook |

`alfred-coo-a` prefers `qwen3-coder:480b-cloud` (deepseek as fallback) because `deepseek-v3.2:cloud` intermittently emits Anthropic-style `<function_calls>` XML in the content field instead of the OpenAI `tool_calls` field once the tool schema exceeds ~4 tools. See the comment above the persona definition and `reference_deepseek_tool_use_quirk.md` in soul memory.

`alfred-coo` is kept as a legacy alias for `alfred-coo-a`.

## Task routing

Task titles are parsed for routing markers in this order:

1. `[persona:<name>]` — exact persona match against `BUILTIN_PERSONAS`.
2. `[unified-plan-wave-1]` — special-case claim marker; routes to `default` persona unless a `[persona:...]` tag is also present.
3. `[tag:code]` — overrides model selection to `qwen3-coder:480b-cloud` regardless of persona preference.
4. `[tag:strategy]` — overrides model selection to `deepseek-v3.2:cloud`.
5. Unknown persona name falls back to `default`.

## Running it

Local dev:

```bash
pip install -e .
python -m alfred_coo.main
```

Required env vars:

- `SOUL_API_KEY` — soul-svc bearer token for mesh + memory.
- `OLLAMA_API_KEY` — Ollama Cloud auth (set on the `OLLAMA_URL` endpoint).
- `LINEAR_API_KEY` (or `ALFRED_OPS_LINEAR_API_KEY`) — for `linear_create_issue`.
- `SLACK_BOT_TOKEN` (or `SLACK_BOT_TOKEN_ALFRED`) — for `slack_post`.
- `GITHUB_TOKEN` — for `propose_pr`.

See `deploy/.env.template` for the full set and defaults.

## Testing

```bash
pytest tests/
```

59 tests across `test_persona.py`, `test_tools.py`, `test_artifacts.py`, `test_structured.py`.

## Status

Shipped:

- v0 scaffold: claim/dispatch/complete loop with fallback URL chain.
- B.1: persona registry + model fallback + topic-scoped memory loading.
- B.2: structured envelope contract + path-safe artifact writer.
- B.3.1-4: OpenAI-compatible tool-use dispatch; `linear_create_issue`, `slack_post`, `mesh_task_create`, `propose_pr` (REST, no `gh` CLI), task-scoped workspaces via ContextVar, `http_get` with strict allowlist.

Phase B-next:

- Anthropic-native tool-use adapter (sidestep the deepseek XML drift).
- Adler breakout validator (detect persona drift vs seasoning on outputs).
- Per-task cost tracker against `DAILY_BUDGET_USD`.
- In-flight recovery (reclaim tasks orphaned mid-dispatch on restart).

## License

PolyForm-Noncommercial-1.0.0. Commercial licensing: `info@saluca.com`.
