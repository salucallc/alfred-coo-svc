# OPS-14D: Enforce 24h TTL on scoped OAuth2 tokens

## Target paths
- src/alfred_coo/auth/scoped_tokens.py
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py
- plans/v1-ga/OPS-14d.md

## Acceptance criteria
* Action and verification as per APE/V block.

## Verification approach
* Unit tests pass; `grep -E "86400|24.*3600" src/alfred_coo/auth/` finds TTL constant.

## Risks
* Minimal risk; changes isolated to validation logic.
