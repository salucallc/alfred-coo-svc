# autonomous_build smoke tests

Two flavours:

## 1. In-process dry-run smoke (CI-safe)

No credentials, no network. Runs `AutonomousBuildOrchestrator.run()` end-to-end with
`DryRunAdapter` swapped in for mesh + Slack + Linear. Gated by `@pytest.mark.smoke`
so regular `pytest` runs skip it.

```bash
AUTONOMOUS_BUILD_DRY_RUN=1 pytest tests/smoke/test_autonomous_build_smoke.py -v -m smoke
```

Windows PowerShell:

```powershell
$env:AUTONOMOUS_BUILD_DRY_RUN="1"; pytest tests\smoke\test_autonomous_build_smoke.py -v -m smoke
```

Expected: 2 tests pass in well under 10 seconds wall-clock. The happy-path test
dispatches a 3-ticket mock project (2 wave-0 + 1 wave-1 ticket blocked on wave-0)
and verifies wave sequencing, dep gating, budget accounting, Slack cadence
emission, and final all-green state.

This runs automatically on pull requests that touch
`src/alfred_coo/autonomous_build/**` via
`.github/workflows/autonomous_build_smoke.yml`.

## 2. Live-scope narrow smoke (operator-only, NOT automated)

Operator territory. Points the deployed orchestrator at a throwaway Linear
sub-project containing 3 real tickets with a $1 budget cap and lets it run
end-to-end against real mesh / real Slack / real GitHub.

### Prerequisites

- A Linear project scoped to 3 small tickets with `wave-0` / `wave-1` labels
  and one `blocked_by` relation between them (mirrors the in-process fixture).
- The project's Linear project UUID.
- A target Slack channel the bot is already a member of.
- Real `LINEAR_API_KEY`, `SLACK_BOT_TOKEN_ALFRED`, and `SOUL_API_KEY` configured
  on the host running the orchestrator (usually Oracle VM).
- The `mesh_task_create` ACL on the deployed daemon allows the operator's session
  to kickoff the persona.

### Command (on Oracle VM, authenticated shell)

```bash
# AUTONOMOUS_BUILD_DRY_RUN must NOT be set.
unset AUTONOMOUS_BUILD_DRY_RUN

# Kickoff via the mesh_task_create MCP tool (or curl direct to soul-svc).
# Payload is the kickoff JSON the orchestrator parses in _parse_payload.
PAYLOAD=$(cat <<'JSON'
{
  "linear_project_id": "<throwaway-project-uuid>",
  "concurrency": {"max_parallel_subs": 2, "per_epic_cap": 2},
  "budget": {"max_usd": 1.00, "warn_threshold_pct": 0.8},
  "wave_order": [0, 1],
  "on_all_green": [],
  "status_cadence": {
    "interval_minutes": 5,
    "slack_channel": "C0ASAKFTR1C",
    "stall_threshold_sec": 600
  }
}
JSON
)

curl -sS -X POST "http://100.105.27.63:8080/v1/mesh/tasks" \
  -H "Authorization: Bearer $SOUL_API_KEY" \
  -H "Content-Type: application/json" \
  --data "$(jq -n --arg desc "$PAYLOAD" --arg sid "alfred-ops" \
      '{from_session_id: $sid, title: "[persona:autonomous-build-a] narrow live smoke", description: $desc}')"
```

### Expected timeline

- ~1 min: orchestrator claims the kickoff, parses payload, builds graph.
- ~2-5 min: 2 wave-0 children fan out; each posts a PR to the target repo.
- ~5-10 min: Wave-0 drains; `hawkman-qa-a` review dispatched per PR.
- ~10-20 min: Wave-1 ticket dispatches after wave-0 merges green.
- End: kickoff task completes with merged_green on all 3.

### Cost budget

$1.00 cap should comfortably cover 3 small tickets. Budget warn fires at $0.80;
hard-stop at $1.00 flips drain mode on. If the run exits early with a
`budget_hard_stop` event in `state.events`, check the model routing — the
orchestrator is probably burning tokens on oversized contexts.

### Teardown

The live smoke leaves Linear issues, mesh tasks, and (if successful) merged
PRs. Clean up manually:

- Close the 3 Linear tickets if the persona didn't already.
- Delete the throwaway sub-project in Linear.
- Revert / delete the merged PRs on the target repo if they were throwaway.

### Go/no-go before running

Before scheduling a live smoke, operator confirms:

1. CI green on `main` for the autonomous_build module.
2. In-process dry-run smoke passes locally.
3. No other autonomous_build kickoff is currently in flight (check
   `mesh_tasks` for persona tag `autonomous-build-a`).
4. Slack channel is dedicated or low-noise; cadence posts will land every
   5 minutes during the run.
5. $1 budget cap matches the target ticket sizes; bump to $2-$3 if any
   ticket is size-L.

If any of those is unclear, default to another dry-run cycle and keep iterating
on the in-process harness first.
