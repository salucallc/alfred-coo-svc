# OPS-14: Scoped OAuth2 client_credentials flow

## Target paths
- `deploy/appliance/authelia/oauth2_clients.yml`
- `src/alfred_coo/auth/scoped_tokens.py`
- `tests/test_scoped_oauth_tokens.py`
- `plans/v1-ga/OPS-14.md`

## Acceptance criteria
- Token with scope soul:memory:read -> 200 on search endpoint, 403 on write; 24h TTL enforced; portal rotation UI issues new token

## Verification approach
- Unit test `tests/test_scoped_oauth_tokens.py` validates token retrieval.
- Manual curl to Authelia token endpoint confirms 200 response and correct TTL.
- Integration test against search endpoint verifies 200 for read scope and 403 for write scope.

## Risks
- Misconfiguration of client secret could lock out services.
- TTL mismatch may cause unexpected token expiration.
- Scope spelling errors could lead to unauthorized access.
