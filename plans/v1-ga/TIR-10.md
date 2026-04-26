# TIR-10: Split docker networks for egress isolation

## Target paths
- deploy/appliance/docker-compose.yml
- deploy/appliance/tiresias/network_split.md
- plans/v1-ga/TIR-10.md

## Acceptance criteria
- APE/V: `docker exec alfred-coo curl --max-time 5 https://api.github.com` fails; `docker exec mcp-github curl` same URL succeeds

## Verification approach
- Run the two Docker exec commands inside the running appliance container stack and verify the expected success/failure.
- CI test script `tests/network_split_test.sh` asserts the exit codes.

## Risks
- Incorrect network assignment could break internal service communication.
- Misconfiguration might allow unwanted internet egress from COO.
- Ensure both `mc-internal` and `mc-egress` bridges are defined and not overlapping with existing `appliance` network.
