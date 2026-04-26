# OPS-10: Authelia file backend

## Target paths
- deploy/appliance/authelia/configuration.yml
- deploy/appliance/authelia/users_database.yml
- deploy/appliance/authelia/README.md

## Acceptance criteria
- `/auth/` renders; admin login returns 200

## Verification approach
- Run `docker compose up -d` and ensure the Authelia service starts.
- Execute `curl -f http://localhost/auth/` and expect HTTP 200.
- Perform admin login via the wizard (Screen 8) and verify a 200 response.

## Risks
- Misconfiguration of file backend could lock out admin access.
- Ensure the admin passphrase is set correctly in the wizard.
