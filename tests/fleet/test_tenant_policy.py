import pytest
from alfred_coo.fleet_policy import tenant_bundles

def test_tenant_bundles_structure():
    assert isinstance(tenant_bundles.tenant_bundles, dict)
    assert "tenant_a" in tenant_bundles.tenant_bundles
    assert "tenant_b" in tenant_bundles.tenant_bundles
