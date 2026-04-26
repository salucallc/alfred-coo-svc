# OPS-15: Wizard Screen 8 (admin + RBAC init)

## Target paths
- deploy/appliance/wizard/screen_8_admin_init.html
- scripts/mc_auth_init_admin.sh

## Acceptance criteria
- Post-wizard admin exists; ./mc.sh auth list-users >=1 row

## Verification approach
- Run the wizard up to screen 8, provide an admin passphrase, and complete.
- Execute `./mc.sh auth list-users` and verify the output contains the admin entry.

## Risks
- Incorrect handling of the passphrase could lock out administrators.
- Storing the passphrase in plaintext in the users file may be a security concern; ensure proper file permissions (600) and consider hashing.
