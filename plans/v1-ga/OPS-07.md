# OPS-07: Infisical agent sidecars

## Target paths
- deploy/appliance/infisical/agent_sidecar.yml
- plans/v1-ga/OPS-07.md

## Acceptance criteria
Each service: cat /run/secrets/<name> matches infisical UI; 3 services verified

## Verification approach
Add unit/integration test that starts the sidecar container, writes a test secret to Infisical, then `cat /run/secrets/<service>_test` returns the same value. Verify for three representative services.

## Risks
- Secret sync latency up to 60s may affect tests.
- Agent image size (~20MB) increases deployment size.
