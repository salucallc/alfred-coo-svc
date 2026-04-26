# ALT-04: Model router + two-tier policy

## Target paths
- aletheia/app/router/__init__.py
- aletheia/app/router/policy.py
- aletheia/tests/test_router.py

## Acceptance criteria
- Given 12 `(action_class, risk_tier)` rows, router returns expected model_id. Refuses when `generator_model == candidate_verifier_model`. Unit tests committed.

## Verification approach
- Unit tests in ``aletheia/tests/test_router.py`` exercise all routing permutations and the generator‑verifier clash guard.
- ``ruff`` linting runs as part of CI; no style violations.

## Risks
- Incorrect mapping could cause verifier/model mismatches leading to unnecessary costs.
- Future action classes must be added to ``_ROUTER_TABLE`` to stay in sync with the plan.
