import pytest
from alfred_coo.fleet_endpoint import quarantine as qz


def test_quarantine_cycle():
    endpoint_id = "ep_test_123"
    # Start in normal mode
    assert qz.get_endpoint_state(endpoint_id) == "normal"
    # Simulate API‑key expiry -> quarantine
    qz.expire_api_key(endpoint_id)
    assert qz.get_endpoint_state(endpoint_id) == "quarantine"
    # Recover using the unquarantine helper
    qz.unquarantine(endpoint_id)
    assert qz.get_endpoint_state(endpoint_id) == "normal"
