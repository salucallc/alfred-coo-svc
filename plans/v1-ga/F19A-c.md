# F19A-c: mcctl push-policy CLI subcommand wiring

**Linear:** SAL-3262

**Parent:** F19A (SAL-3070) -- see `plans/v1-ga/F19A-c.md`
**Wave:** 3
**Auto-dispatchable:** yes

## Context

Wires the `mcctl push-policy <bundle-path>` subcommand. Calls the F19A-a helper (`_upload_bundle`) to compute sha256+payload, then HTTP POSTs to the F19A-b endpoint (`POST /v1/fleet/policy-bundles`). On success prints `pushed policy <name>@<version> sha256=<hash>` and exits 0. On 409 prints a friendly duplicate-version message and exits 1.

Child of SAL-3070 -- depends on F19A-a (helper) and F19A-b (endpoint) being callable. Tests use HTTP mocks, no live hub.

## APE/V Acceptance (machine-checkable)

1. `mcctl push-policy --help` exits 0 and shows the `<bundle-path>` positional arg.
2. File `cmd/mcctl/push_policy.py` exists and the subcommand is registered in the mcctl entrypoint (search the existing CLI registration pattern).
3. File `tests/cmd/mcctl/test_push_policy.py` exists with at least `test_push_policy_success` and `test_push_policy_duplicate_409` (using HTTP mocking, no live network).
4. `pytest tests/cmd/mcctl/test_push_policy.py -v` exits 0.

## Out of scope

- Live hub round-trip -- F19A-d.
