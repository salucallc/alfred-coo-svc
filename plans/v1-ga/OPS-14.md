# OPS-14: Scoped OAuth2 API tokens

## Target paths
- deploy/appliance/authelia/oauth2_clients.yml
- src/alfred_coo/auth/scoped_tokens.py
- tests/test_scoped_oauth_tokens.py

## Acceptance criteria
Token with scope soul:memory:read -> 200 on search endpoint, 403 on write; 24h TTL enforced; portal rotation UI issues new token

## Verification approach
Unit test validates token retrieval; manual curl to Authelia confirms scopes and TTL; portal UI rotation creates fresh token.

## Risks
- Secret leakage if client_secret is hard‑coded (mitigated by env injection)
- TTL enforcement relies on Authelia config correctness
