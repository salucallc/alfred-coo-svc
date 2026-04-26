# OPS-23: Add model pricing loader and tests

## Target paths
- src/alfred_coo/__init__.py
- configs/model_pricing.yaml
- src/alfred_coo/pricing.py
- tests/test_pricing_loader.py
- plans/v1-ga/OPS-23.md

## Acceptance criteria
- Address every point in the review feedback below.
- Tests still green (`ruff` + `pytest`).
- Push fixes to the EXISTING branch for https://github.com/salucallc/alfred-coo-svc/pull/121 via the `update_pr` tool; do NOT open a new PR. The reviewer bot will re-review automatically once your new commit lands.

## Verification approach
- Run `pytest` to ensure the new test passes.
- Run `ruff` to ensure no lint errors.
- Verify the pricing loader returns a dictionary with expected keys.

## Risks
- Introducing a new config directory may affect deployment scripts if not accounted for.
- YAML parsing errors could break the loader; covered by tests.
