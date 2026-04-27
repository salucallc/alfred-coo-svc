# OPS-15: Wizard Screen 8 – admin + RBAC init

## Target paths
- deploy/appliance/wizard/screen_8_admin_init.html  # will CREATE this file
- scripts/mc_auth_init_admin.sh                    # will CREATE this file
- plans/v1-ga/OPS-15.md                            # this plan doc

## Acceptance criteria
- Implementation matches the plan section for this ticket.
- Unit + integration tests added or updated.
- `ruff` + `pytest` green in CI.
- PR opened via `propose_pr`; orchestrator will dispatch a hawkman-qa-a review on merge-ready.
- Structured output envelope includes the PR URL in `summary` or `follow_up_tasks`.

## Verification approach
- Run the wizard in a fresh appliance install, fill in the admin passphrase on Screen 8.
- Verify that `./mc.sh auth list-users` returns at least one row containing the admin user.
- Ensure the new HTML renders correctly via the wizard UI and the script exits with status 0.
- Add unit test for `scripts/mc_auth_init_admin.sh` parsing JSON and invoking `./mc.sh auth add-admin`.

## Risks
- Incorrect passphrase handling could lock out admin; script validates non‑empty input.
- HTML form may require CORS adjustments if served from a different origin.
- Dependency on `jq` and `./mc.sh` being present in the container runtime.
