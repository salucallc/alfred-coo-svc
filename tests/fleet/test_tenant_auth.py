import pytest

from alfred_coo.fleet_auth.tenant_binding import TenantBinding

@pytest.fixture
def dummy_request():
    class Req:
        def __init__(self):
            self.context = {}
    return Req()

def test_tenant_binding_assigns_id(dummy_request):
    tb = TenantBinding('tenant-123')
    req = tb.bind(dummy_request)
    assert req.context['tenant_id'] == 'tenant-123'
