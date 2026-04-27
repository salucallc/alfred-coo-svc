# TIR-14: E2E sovereignty smoke test CI

## Target paths
- .github/workflows/tiresias_sovereignty_smoke.yml
- deploy/appliance/tiresias/smoke_test.sh
- deploy/appliance/tiresias/test_audit_chain_walk.py

## Acceptance criteria
- APE/V: new workflow asserts (1) smoke_test.sh passes, (2) direct api.github.com from coo fails, (3) proxied call succeeds + audit, (4) unregistered soulkey → 403 P1, (5) audit chain walk verifies all links

## Verification approach
- GitHub Actions workflow runs on PRs and ensures the smoke test script exits with status 0.
- The Python pytest suite validates that the audit chain contains no hash-link breaks across ≥10 rows (simulated in CI).
- Manual verification can be performed by executing `deploy/appliance/tiresias/smoke_test.sh` against a local docker-compose environment.

## Risks
- Reliance on Docker container names (`alfred-coo`) may change.
- Direct network failure detection may be flaky in CI environments.
- Placeholder implementations need real service endpoints for full verification.
