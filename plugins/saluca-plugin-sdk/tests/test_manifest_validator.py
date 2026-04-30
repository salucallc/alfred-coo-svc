import pytest
import yaml
from saluca_plugin_sdk.manifest_validator import validate_external_agent, validate_external_surface

def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)

def test_valid_external_agent():
    data = load_yaml("plugins/saluca-plugin-sdk/tests/fixtures/external_agent_manifest.yaml")
    obj = validate_external_agent(data)
    assert obj.name == "example-agent"

def test_invalid_scope_raises():
    data = {"name": "bad", "scope": "unknown", "version": "1.0"}
    with pytest.raises(ValueError):
        validate_external_agent(data)
