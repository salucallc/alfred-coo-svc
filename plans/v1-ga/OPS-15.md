# OPS-15: Wizard Screen 8 – Admin + RBAC init

## Target paths
- deploy/appliance/wizard/screen_8_admin_init.html
- scripts/mc_auth_init_admin.sh
- plans/v1-ga/OPS-15.md

## Acceptance criteria
- Post-wizard admin exists; `./mc.sh auth list-users` >=1 row

## Verification approach
1. Run the wizard and provide a passphrase.
2. Execute `scripts/mc_auth_init_admin.sh <passphrase>`.
3. Verify that `./mc.sh auth list-users` lists the admin user (at least one row).
4. Access the new wizard screen via the UI and confirm the form submits without error.

## Risks
- Admin passphrase may be stored insecurely if logs are not sanitized.
- If the script fails, the wizard could leave the appliance in a partially configured state.
