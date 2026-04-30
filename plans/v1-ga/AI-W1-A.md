# AI-W1-A: saluca-plugin-sdk PyPI package

## Target paths
- plugins/saluca-plugin-sdk/pyproject.toml
- plugins/saluca-plugin-sdk/saluca_plugin_sdk/__init__.py
- plugins/saluca-plugin-sdk/saluca_plugin_sdk/agent_plugin.py
- plugins/saluca-plugin-sdk/saluca_plugin_sdk/dataclasses.py
- plugins/saluca-plugin-sdk/saluca_plugin_sdk/manifest_validator.py
- plugins/saluca-plugin-sdk/saluca_plugin_sdk/audit_hook.py
- plugins/saluca-plugin-sdk/saluca_plugin_sdk/conftest.py
- plugins/saluca-plugin-sdk/tests/test_abc_conformance.py
- plugins/saluca-plugin-sdk/tests/test_manifest_validator.py
- plugins/saluca-plugin-sdk/tests/fixtures/external_agent_manifest.yaml
- plugins/saluca-plugin-sdk/tests/fixtures/external_surface_manifest.yaml
- plugins/saluca-plugin-sdk/README.md
- plugins/saluca-plugin-sdk/LICENSE

## Acceptance criteria
**Acceptance:**

1. `pip install saluca-plugin-sdk==0.1.0` succeeds from public PyPI.
2. `from saluca_plugin_sdk import SalucaPlugin, AgentCapabilities, RegistrationResult, DispatchResult, AuditEvent` resolves.
3. CI gate validates that any plugin-class declaring `direction in {inbound, bidirectional}` overrides `dispatch_inbound`; same for `dispatch_outbound`.
4. Manifest validator accepts both reference manifests (one `ExternalAgent`, one `ExternalSurface` from §5 of plan doc) and rejects manifests with bad scope enums or missing required fields with structured errors.

## Verification approach
Run `pytest -q` in the plugin SDK directory; ensure all tests pass and the package builds with `python -m build` then `pip install dist/*.whl`.

## Risks
- Compatibility with future Saluca core versions.
- Missing runtime dependencies for requests/HMAC.
