# OPS-14: Scoped OAuth2 client credentials for Authelia

## Target paths
- deploy/appliance/authelia/oauth2_clients.yml
- src/alfred_coo/auth/scoped_tokens.py
- tests/test_scoped_oauth_tokens.py

## Acceptance criteria
- Token with scope soul:memory:read -> 200 on search endpoint, 403 on write; 24h TTL enforced; portal rotation UI issues new token

## Verification approach
- Unit tests validate token generation, scope enforcement, TTL expiry, and portal rotation behavior.
- Manual integration test against Authelia service confirming 200/403 responses.

## Risks
- Incorrect scope handling may expose write access.
- TTL misconfiguration could lead to token leakage.
- Compatibility with existing Authelia configuration.
