import pytest
from saluca_plugin_sdk.manifest_validator import validate_manifest

valid_agent = {
    "kind": "ExternalAgent",
    "agent_id": "agent123",
    "capabilities": ["read", "write"],
    "scope": "global",
}

invalid_agent = {
    "kind": "ExternalAgent",
    "agent_id": "agent123",
    "capabilities": ["read"],
    "scope": "unknown",
}

valid_surface = {
    "kind": "ExternalSurface",
    "surface_id": "surf456",
    "description": "test surface",
    "scope": "local",
}

invalid_surface = {
    "kind": "ExternalSurface",
    "surface_id": "surf456",
    "description": "test",
    "scope": "bad",
}


def test_valid_manifests():
    assert validate_manifest(valid_agent) == []
    assert validate_manifest(valid_surface) == []


def test_invalid_manifests():
    errors = validate_manifest(invalid_agent)
    assert any("invalid scope enum" in e for e in errors)
    errors2 = validate_manifest(invalid_surface)
    assert any("invalid scope enum" in e for e in errors2)
