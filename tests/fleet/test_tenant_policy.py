# SPDX-License-Identifier: MIT
"""Unit tests for per‑tenant policy bundle handling.
The tests are deliberately lightweight – they confirm the static bundle
structure matches the acceptance criteria for C‑28.
"""

import pytest
from alfred_coo.fleet_policy import get_tenant_bundle, list_tenants


def test_known_tenants_present():
    tenants = set(list_tenants())
    assert tenants == {"tenant_a", "tenant_b"}


def test_bundle_allowlist_contents():
    a = get_tenant_bundle("tenant_a")
    b = get_tenant_bundle("tenant_b")
    assert "mcp.github.read" in a["bundle"]["tool_allowlist"]
    assert "mcp.linear.write" in b["bundle"]["tool_allowlist"]
    # Ensure cross‑tenant restrictions hold (simulated)
    assert "mcp.linear.write" not in a["bundle"]["tool_allowlist"]
    assert "mcp.github.read" not in b["bundle"]["tool_allowlist"]

def test_invalid_tenant_raises():
    with pytest.raises(KeyError):
        get_tenant_bundle("nonexistent")
