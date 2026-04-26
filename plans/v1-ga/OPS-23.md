# OPS-23: Model pricing loader

## Target paths
- configs/model_pricing.yaml
- src/alfred_coo/pricing.py
- tests/test_pricing_loader.py

## Acceptance criteria
`pricing.load()['openrouter/free']['input_per_1k']` returns 0.0

## Verification approach
Run `pytest -q` to ensure the test passes and manually verify the loader returns the expected value.

## Risks
No significant risks; assumes PyYAML is available.
