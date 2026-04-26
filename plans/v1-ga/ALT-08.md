# ALT-08: MCP preflight wrapper

## Target paths
- aletheia/app/preflight/__init__.py
- aletheia/app/preflight/server.py
- aletheia/tests/test_preflight.py

## Acceptance criteria
- APE/V: Patched `mcp-slack` sends test msg; Aletheia preflight POST returns `PASS`/`FAIL`. Forge preflight to non-existent channel → `FAIL`, MCP aborts with HTTP 412. Log in soul-svc.

## Verification approach
- Unit tests in `aletheia/tests/test_preflight.py` verify that a non‑existent channel triggers a 412 response (FAIL) and a valid channel returns PASS.
- Manual curl test can also confirm the behaviour.

## Risks
- The stub treats only the literal string "nonexistent" as invalid; real implementation will need proper Slack channel validation.
- Ensure the endpoint is included in the service's router configuration.
