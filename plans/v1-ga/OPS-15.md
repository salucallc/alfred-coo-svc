# OPS-15: Wizard Screen 8 – admin + RBAC init

## Target paths
- deploy/appliance/wizard/screen_8_admin_init.html
- scripts/mc_auth_init_admin.sh

## Acceptance criteria
Post-wizard admin exists; `./mc.sh auth list-users` >=1 row

## Verification approach
Add script and HTML, run wizard, then execute `./mc.sh auth list-users` expecting at least one admin entry.

## Risks
- Secret handling of admin passphrase.
- Script may fail on missing dependencies.