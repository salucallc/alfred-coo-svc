# OPS-11: Caddy forward_auth for /ops/*

## Target paths
- deploy/appliance/Caddyfile

## Acceptance criteria
- Unauth → 302 /auth
- Session cookie → 200

## Verification approach
- Run `curl -I http://localhost/ops/` without auth cookie, expect HTTP 302 redirect to `/auth`.
- Run `curl -I --cookie "session=valid" http://localhost/ops/`, expect HTTP 200.
- CI includes integration tests verifying these responses.

## Risks
- Misconfiguration of `forward_auth` could block all `/ops/*` traffic.
- Dependency on Authelia service availability; network issues may cause 502 errors.
