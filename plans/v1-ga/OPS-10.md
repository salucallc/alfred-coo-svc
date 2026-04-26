# OPS-10: Authelia file backend integration

## Target paths
- deploy/appliance/docker-compose.yml
- deploy/appliance/authelia/configuration.yml (new)
- deploy/appliance/authelia/users_database.yml (new)
- deploy/appliance/authelia/README.md (new)

## Acceptance criteria
- "/auth/" renders
- admin login returns 200

## Verification approach
- Run `docker compose up` and access `http://localhost/auth/` expecting a login page.
- Use wizard admin credentials to log in; expect HTTP 200 response.

## Risks
- Misconfiguration of file backend could lock out admin access.
- Ensure proper volume mounts for configuration and user database files.
