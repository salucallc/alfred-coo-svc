import pytest
import yaml
from saluca_plugin_sdk.manifest_validator import validate_external_agent, validate_external_surface, ValidationError

def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)

def test_valid_manifests():
    agent = load_yaml("plugins/saluca-plugin-sdk/tests/fixtures/external_agent_manifest.yaml")
    surface = load_yaml("plugins/saluca-plugin-sdk/tests/fixtures/external_surface_manifest.yaml")
    assert validate_external_agent(agent)
    assert validate_external_surface(surface)

def test_invalid_manifest():
    bad = {"kind": "ExternalAgent", "agent_id": 123}
    with pytest.raises(ValidationError):
        validate_external_agent(bad)
