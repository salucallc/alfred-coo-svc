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
| `pr_review` | Submit PR review (APPROVE / REQUEST_CHANGES / COMMENT) | `GITHUB_TOKEN` | Org allowlist (same as `propose_pr`). Supports overall body + optional inline line comments. Used by verifier personas (`hawkman-qa-a`, `batgirl-sec-a`) that review code they did not build. |
| `pr_files_get` | Fetch all files in a PR with content at head SHA | `GITHUB_TOKEN` | Org allowlist. 20 KB cap per file, 50-file cap per PR (truncation marked explicitly). |
| `slack_ack_poll` | Poll a Slack channel for the first ACK message from one author | `SLACK_BOT_TOKEN_ALFRED` | Used by the `autonomous-build-a` SS-08 gate. Regex keywords matched case-insensitive; paginates via `response_metadata.next_cursor`. **Requires bot scope `channels:history`** (and `users:read.email` so callers can resolve the approver's user id via `users.lookupByEmail`). |
| `linear_update_issue_state` | Transition a Linear issue to a named workflow state | `LINEAR_API_KEY` | Scoped per-team (state IDs aren't global). Accepts UUID or identifier (e.g. `SAL-2680`). Team-states map cached in-process. |
| `linear_list_project_issues` | List all issues in a Linear project with labels / estimate / state / relations | `LINEAR_API_KEY` | Paginates `issues(first: 100)`; default cap 250. Orchestrator uses this to build the wave + dependency graph. |
| `linear_get_issue_relations` | Bucket relations for one issue into blocks / blocked_by / related | `LINEAR_API_KEY` | Unknown relation types fall into `related` so dependency info isn't dropped. |

### Slack bot scopes required

The `slack_post` tool needs `chat:write`. `slack_ack_poll` additionally needs
**`channels:history`** (to read messages in the target channel) and
**`users:read.email`** (to resolve an approver's user id via
`users.lookupByEmail`). These two scopes must be added in the Slack app
settings for the `SLACK_BOT_TOKEN_ALFRED` bot. After the scope change the
operator must reinstall the app to the workspace and refresh the bot token.
The repository does not vendor a Slack app manifest file — scope management
lives in the Slack app dashboard.

## Personas

| Name | Preferred | Fallback | Tools | Topics |
|---|---|---|---|---|
| `default` | `deepseek-v3.2:cloud` | `qwen3-coder:480b-cloud` | (none) | (none) |
| `alfred-coo-a` | `qwen3-coder:480b-cloud` | `deepseek-v3.2:cloud` | linear, slack, mesh, propose_pr, http_get | coo-daemon, unified-plan, gap-closure, mission-control, autonomous-ops |
| `riddler-crypto-a` | `qwen3-coder:480b-cloud` | `deepseek-v3.2:cloud` | linear, slack, mesh, propose_pr, http_get | pq, sovereign-pq, crypto, karolin-sovereign-pq, cryptography, ciphers |
| `hawkman-qa-a` | `qwen3-coder:480b-cloud` | `deepseek-v3.2:cloud` | http_get, pr_review, slack, linear | qa, test, coverage, acceptance-criteria, regression, verification |
| `batgirl-sec-a` | `qwen3-coder:480b-cloud` | `deepseek-v3.2:cloud` | http_get, pr_review, slack, linear | security, attack-vector, zero-trust, pr-review, code-review, allowlist |
| `batman-ciso-a` | `deepseek-v3.2:cloud` | `qwen3-coder:480b-cloud` | slack, linear | ciso, security-architecture, threat-model, red-team, siem, incident-response |
| `steel-cto-a` | `deepseek-v3.2:cloud` | `qwen3-coder:480b-cloud` | slack, linear | cto, engineering, architecture, roadmap, platform |
| `maxwell-lord-a` | `deepseek-v3.2:cloud` | `qwen3-coder:480b-cloud` | (none) | stripe, pricing, onboarding, revenue, funnel, billing, sales |
| `starfire-ventures-a` | `deepseek-v3.2:cloud` | `qwen3-coder:480b-cloud` | (none) | impulse, ventures, arb-bot, trading, market-research |
| `lucius-fox-a` | `deepseek-v3.2:cloud` | `qwen3-coder:480b-cloud` | (none) | patent, fundraising, investor, investment, ip, finance, cfo |
| `sawyer-ops-a` | `deepseek-v3.2:cloud` | `qwen3-coder:480b-cloud` | (none) | audit, pipeline, operations, deploy, runbook, compliance, privacy |
| `red-robin-a` | `deepseek-v3.2:cloud` | `qwen3-coder:480b-cloud` | (none) | twin-rho, mnemosyne, hypnos, ahi, innovation, research, r-and-d |

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

Regular `pytest` runs skip the `@pytest.mark.smoke` end-to-end harness for the
`autonomous-build-a` persona. To exercise the dry-run smoke explicitly:

```bash
AUTONOMOUS_BUILD_DRY_RUN=1 pytest tests/smoke/test_autonomous_build_smoke.py -v -m smoke
```

The smoke test runs the full orchestrator `run()` loop in-process against a
mocked Linear project with `DryRunAdapter` swapped in for mesh / Slack /
Linear. No credentials or network required. CI runs it automatically on PRs
that touch `src/alfred_coo/autonomous_build/**` (see
`.github/workflows/autonomous_build_smoke.yml`). See `tests/smoke/README.md`
for the operator's live-scope narrow-smoke procedure (real Linear, real
Slack, $1 budget cap — do NOT automate).

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
