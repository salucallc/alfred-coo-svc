import pytest
from src.alfred_coo.agent_ingest.direction_router import route_task

def test_route_inbound():
    result = route_task("inbound", {"payload": 1})
    assert result["status"] == "inbound_handled"

def test_route_outbound():
    result = route_task("outbound", {"payload": 2})
    assert result["status"] == "outbound_handled"

def test_invalid_direction():
    with pytest.raises(ValueError):
        route_task("unknown", {})
