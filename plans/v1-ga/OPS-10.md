# OPS-10: Authelia file backend configuration

## Target paths
- deploy/appliance/authelia/configuration.yml
- deploy/appliance/authelia/users_database.yml
- deploy/appliance/authelia/README.md
- plans/v1-ga/OPS-10.md

## Acceptance criteria
/auth/ renders; admin login returns 200

## Verification approach
- Deploy the updated compose with the new Authelia configuration mounted at `/config`.
- Perform an HTTP GET request to `http://<host>/auth/` and confirm a 200 response.
- Log in using the `admin` credentials defined in `users_database.yml` and verify a successful login (HTTP 200).

## Risks
- Placeholder password hash must be replaced before production deployment.
- Session secret must be provided via the `AUTHELIA_SESSION_SECRET` environment variable.
- File backend is not suitable for large user bases; consider migrating to an external IdP later.
