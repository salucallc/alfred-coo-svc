# OPS-23: Model pricing loader

## Target paths
- configs/model_pricing.yaml
- src/alfred_coo/pricing.py
- tests/test_pricing_loader.py

## Acceptance criteria
- `pricing.load()['openrouter/free']['input_per_1k']` returns 0.0

## Verification approach
- Unit test `tests/test_pricing_loader.py` asserts the value.
- Manual import of `alfred_coo.pricing.load` returns expected dict.

## Risks
- Missing `configs` directory may cause load failure; ensure path resolution uses repository root.
- YAML syntax errors break loading; validation via unit test.
