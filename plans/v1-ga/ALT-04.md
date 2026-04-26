# ALT-04: Model router + two-tier policy

## Target paths
- aletheia/app/router/__init__.py
- aletheia/app/router/policy.py
- aletheia/tests/test_router.py

## Acceptance criteria
Given 12 `(action_class, risk_tier)` rows, router returns expected model_id. Refuses when `generator_model == candidate_verifier_model`. Unit tests committed.

## Verification approach
Run the pytest suite; all tests must pass confirming correct routing and refusal behavior.

## Risks
- Routing table may drift from specification; ensure updates keep tests in sync.
- New action classes require extending the routing table and tests.
