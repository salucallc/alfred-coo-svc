# OPS-14: Scoped OAuth2 API tokens

## Target paths
- deploy/appliance/authelia/oauth2_clients.yml
- src/alfred_coo/auth/scoped_tokens.py
- tests/test_scoped_oauth_tokens.py
- plans/v1-ga/OPS-14.md

## Acceptance criteria
Token with scope soul:memory:read -> 200 on search endpoint, 403 on write; 24h TTL enforced; portal rotation UI issues new token

## Verification approach
- Unit test `tests/test_scoped_oauth_tokens.py` validates read returns 200 and write returns 403 for a read‑only token.
- Manual `curl` against the token endpoint confirms a 24h `expires_in` value.
- Portal UI rotation endpoint generates a new token; its `expires_at` is ~24h ahead.

## Risks
- Secret handling for client secret – must be stored in environment, not in repo.
- Token TTL enforcement relies on Authelia configuration; ensure `token_ttl: 24h` is respected.
- Scope validation may be bypassed if Authelia mis‑configures policy; add integration test later.
