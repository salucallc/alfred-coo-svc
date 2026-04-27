# OPS-23: Model pricing loader

## Target paths
- configs/model_pricing.yaml
- src/alfred_coo/pricing.py
- tests/test_pricing_loader.py
- plans/v1-ga/OPS-23.md

## Acceptance criteria
- `pricing.load()['openrouter/free']['input_per_1k']` returns `0.0`

## Verification approach
- Run `pytest -q tests/test_pricing_loader.py`; test should pass.
- Manual check: load the config and verify the free tier values.

## Risks
- Missing `PyYAML` dependency may cause import errors.
- Incorrect relative path resolution in `pricing.load_pricing`.
- Overwriting an existing config file (none exists currently).
