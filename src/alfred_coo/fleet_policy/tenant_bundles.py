# SPDX-License-Identifier: MIT
"""Static per-tenant policy bundles.
Each bundle is a dict containing a dummy signed payload for illustration.
In production these would be loaded from a secure store.
"""

# Example static bundles – keys are tenant identifiers.
_TENANT_BUNDLES = {
    "tenant_a": {
        "policy_version": "1.0.1",
        "bundle": {
            "tool_allowlist": ["mcp.github.read"],
            "signed": "dummy-sig-a"
        }
    },
    "tenant_b": {
        "policy_version": "1.0.0",
        "bundle": {
            "tool_allowlist": ["mcp.linear.write"],
            "signed": "dummy-sig-b"
        }
    }
}


def get_tenant_bundle(tenant_id: str):
    """Return the policy bundle for *tenant_id*.
    Raises ``KeyError`` if the tenant is unknown.
    """
    return _TENANT_BUNDLES[tenant_id]


def list_tenants():
    """Return a list of known tenant identifiers."""
    return list(_TENANT_BUNDLES.keys())
