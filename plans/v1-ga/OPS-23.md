# OPS-23: model_pricing.yaml loader

## Target paths
- configs/model_pricing.yaml
- src/alfred_coo/pricing.py
- tests/test_pricing_loader.py
- plans/v1-ga/OPS-23.md

## Acceptance criteria
- `pricing.load()['openrouter/free']['input_per_1k']` returns `0.0`

## Verification approach
- Run `pytest tests/test_pricing_loader.py` and ensure it passes.

## Risks
- Requires `pyyaml` to be available in the runtime environment.
- Loader uses a relative path; changes to package layout could break it.
