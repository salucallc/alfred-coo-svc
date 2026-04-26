# tenant_bundles.py
"""
Per-tenant policy bundles.
Each entry maps a tenant_id to a signed bundle placeholder.
"""

tenant_bundles = {
    "tenant_a": {"policy_version": "1.0.0", "bundle": "signed_bundle_a"},
    "tenant_b": {"policy_version": "1.0.0", "bundle": "signed_bundle_b"},
}
