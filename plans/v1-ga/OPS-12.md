# OPS-12: BYO OIDC template + docs

## Target paths
- deploy/appliance/authelia/oidc_clients_template.yml
- deploy/appliance/authelia/BYO_OIDC.md
- scripts/mc_auth_add_oidc.sh
- plans/v1-ga/OPS-12.md

## Acceptance criteria
- `./mc.sh auth add-oidc google --client-id X` adds config; test IdP login works

## Verification approach
Create the OIDC client template and documentation, add the helper script, then execute the command above and manually verify that login via the specified IdP succeeds.

## Risks
- OIDC client configuration syntax errors could break Authelia.
- The helper script must be executable and correctly locate `mc.sh`.
