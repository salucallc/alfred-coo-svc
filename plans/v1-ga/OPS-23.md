# OPS-23: Model Pricing Loader

## Target paths
- configs/model_pricing.yaml
- src/alfred_coo/pricing.py
- tests/test_pricing_loader.py
- plans/v1-ga/OPS-23.md

## Acceptance criteria
- `pricing.load()['free']['input_per_1k']` returns `0.0`.
- `pricing.load()['ollama_max']['flat_monthly_usd']` returns `100`.

## Verification approach
Run the test suite `pytest -q tests/test_pricing_loader.py` and ensure all tests pass.

## Risks
- YAML parsing errors if the config file is malformed.
- Path resolution may break in non‑standard execution environments.
