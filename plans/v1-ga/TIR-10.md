# TIR-10: Split Docker Networks

## Target paths
- deploy/appliance/docker-compose.yml
- deploy/appliance/tiresias/network_split.md
- plans/v1-ga/TIR-10.md

## Acceptance criteria
- [ ] Implementation matches the plan section for this ticket.
- [ ] Unit + integration tests added or updated.
- [ ] `ruff` + `pytest` green in CI.
- [ ] PR opened via `propose_pr`; orchestrator will dispatch a hawkman-qa-a review on merge-ready.
- [ ] Structured output envelope includes the PR URL in `summary` or `follow_up_tasks`.

## Verification approach
- Run `docker compose up` and verify `docker exec alfred-coo curl --max-time 5 https://api.github.com` fails (no internet).
- Verify `docker exec mcp-github curl https://api.github.com` succeeds (egress works).
- CI tests pass; lint passes.

## Risks
- Misconfiguration could block internal service communication.
- Fallback iptables rules may be required per TIR-09 if DNS breaks.
