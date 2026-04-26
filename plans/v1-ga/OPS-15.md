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
Run `./mc.sh auth init-admin --passphrase <test>` using the new script, then execute `./mc.sh auth list-users` and verify the output contains at least one user row.

## Risks
- Incorrect passphrase handling could lock out admin; ensure script validates input.
- Adding new HTML page may require Caddy routing updates; verify routes during integration testing.
