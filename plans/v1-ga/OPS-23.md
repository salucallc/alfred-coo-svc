# OPS-23: Model pricing loader

## Target paths
- src/alfred_coo/pricing.py
- configs/model_pricing.yaml
- tests/test_pricing_loader.py
- plans/v1-ga/OPS-23.md

## Acceptance criteria
- Implementation matches the plan section for this ticket.
- Unit + integration tests added or updated.
- `ruff` + `pytest` green in CI.
- `pricing.load()` returns a dict with expected pricing values (e.g., `openrouter.free.input_per_1k == 0.0`).

## Verification approach
- Run `pytest` to ensure the new test passes.
- Run `ruff` to ensure code style passes.
- Manual check: `python -c "import src.alfred_coo.pricing as p; print(p.load())"` returns the expected dictionary.

## Risks
- None; only adding new files.