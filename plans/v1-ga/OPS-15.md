# OPS-15: Wizard Screen 8 – admin + RBAC init

## Target paths
- deploy/appliance/Caddyfile
- deploy/appliance/authelia/configuration.yml
- deploy/appliance/wizard/screen_8_admin_init.html
- scripts/mc_auth_init_admin.sh
- plans/v1-ga/OPS-15.md

## Acceptance criteria
- Post-wizard admin exists; `./mc.sh auth list-users` ≥1 row

## Verification approach
- Run the wizard up to screen 8, provide a passphrase, and execute `scripts/mc_auth_init_admin.sh`.
- After completion, invoke `./mc.sh auth list-users` and assert that at least one admin row is present.
- Automated test added to `tests/integration/test_wizard_admin.py` verifies the end‑to‑end flow.

## Risks
- No deletions of existing files, only additions.
- Ensure the passphrase handling does not log sensitive data.
- Compatibility with existing Authelia file‑backend configuration.
