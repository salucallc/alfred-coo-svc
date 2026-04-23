# alfred-coo-svc — Oracle VM deploy

Production host: `100.105.27.63`, service user `alfredcoo`, systemd unit `alfred-coo.service`.

## Prereqs

Environment file lives at `/etc/alfred-coo/.env` (mode `0640`, owner `root:alfredcoo`). Template: `deploy/.env.template`.

Required keys (pull from GCP Secret Manager project `salucainfrastructure` unless noted):

| Env var | Secret Manager name | Notes |
|---|---|---|
| `SOUL_API_KEY` | `soul-api-key` | Oracle primary, Cloud Run failover chain in `SOUL_API_URLS`. |
| `GITHUB_TOKEN` | `alfred-coo-github-token` | Repo scope for `propose_pr`; must include `salucallc`, `saluca-labs`, `cristianxruvalcaba-coder`. |
| `LINEAR_API_KEY` | `alfred-ops-linear-api-key` | SAL team. `ALFRED_OPS_LINEAR_API_KEY` is accepted as alias. |
| `SLACK_BOT_TOKEN` | `alfred-slack-bot-token` | `SLACK_BOT_TOKEN_ALFRED` is accepted as alias. |
| `OLLAMA_API_KEY` | `ollama-cloud-api-key` | Consumed by the Ollama Cloud proxy at `OLLAMA_URL`. |
| `ANTHROPIC_API_KEY` | `anthropic-api-key` | Only used when a persona selects a `claude-*` model. |
| `OPENROUTER_API_KEY` | `openrouter-api-key` | Only used for `openrouter/*` models. |

## systemd unit

Canonical unit file: [`deploy/alfred-coo.service`](alfred-coo.service). Installed to `/etc/systemd/system/alfred-coo.service` by `deploy/deploy.sh`.

Hardening that matters:

- `ProtectHome=true` — no access to `/home` or `/root`. This is why `propose_pr` uses `urllib` directly against `https://api.github.com/repos/{owner}/{repo}/pulls`: the `gh` CLI needs `~/.config/gh` for its auth state, which is unreachable under `ProtectHome`. Do not re-introduce a `gh` dependency.
- `ProtectSystem=strict` — filesystem is read-only except for paths listed in `ReadWritePaths`.
- `ReadWritePaths=/var/log/alfred-coo /var/lib/alfred-coo` — workspaces + logs. If you add a new on-disk surface (cost tracker, task-cache db, etc.) extend this list.
- `NoNewPrivileges=true`, `PrivateTmp=true` — standard.
- `User=alfredcoo`, `Group=alfredcoo` — unprivileged service user created by `deploy.sh`.

## Workspace dir

`/var/lib/alfred-coo/workspaces/` must exist and be owned by `alfredcoo`. `deploy.sh` creates `/var/lib/alfred-coo` at install time; the `workspaces/` subdir is created lazily on first `propose_pr` invocation.

Each task gets its own subdir keyed by `task_id` (set via `ContextVar` in `main.py` around the `call_with_tools` invocation, consumed by `propose_pr` via `get_current_task_id()` — see B.3.3). The per-task path is `/var/lib/alfred-coo/workspaces/<task_id>/<repo>/`; a re-run wipes the prior checkout for determinism.

Override root via `ALFRED_WORKSPACES_ROOT` if you relocate state.

## Deploy cycle

```bash
ssh -i ~/.ssh/oci-saluca ubuntu@100.105.27.63
sudo -iu alfredcoo -- bash -lc 'cd /opt/alfred-coo && git pull'
sudo systemctl restart alfred-coo
sudo journalctl -fu alfred-coo
```

First install or config rewrite: run `sudo /opt/alfred-coo/deploy/deploy.sh` (idempotent). Use `--dry-run` to preview.

Health check: `curl http://localhost:8090/healthz` (port from `HEALTH_PORT`).

## Troubleshooting

1. **deepseek-v3.2 tool-use drift.** If `alfred-coo-a` task completions show tool-call XML in the `content` field instead of a real tool-use loop, the primary model is drifting. Flip the persona's `preferred_model` in `src/alfred_coo/persona.py` to `qwen3-coder:480b-cloud` (this is already the default post-#9), redeploy, and restart the service. Root cause: deepseek emits Anthropic `<function_calls>` XML as content once the OpenAI tool schema exceeds ~4 tools; see `reference_deepseek_tool_use_quirk.md` in soul memory.

2. **soul-svc 404 on private-repo `http_get`.** Expected. The `http_get` handler sends no Authorization header (documented on the handler), so private-repo URLs on `github.com` and `raw.githubusercontent.com` return 404. This is by design — do not add a token to the allowlisted public-read handler. For private-repo reads, add a dedicated authenticated tool.

3. **Linear 500 on cancel operations.** Transient Linear GraphQL instability on state-change mutations (observed on `issueUpdate` setting state → `cancelled`). Retry once; if still 500, hit the GraphQL endpoint directly with the same payload (`https://api.linear.app/graphql`) or update via the Linear UI. No daemon-side fix; not a daemon bug.
