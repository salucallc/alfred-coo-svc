import pytest
from src.alfred_coo.agent_ingest.register import register_plugin, RegistrationResult

@pytest.mark.parametrize(
    "direction,expected",
    [
        ("inbound", "inbound"),
        ("outbound", "outbound"),
        ("bidirectional", "bidirectional"),
    ],
)
def test_register_plugin_success(direction):
    result = register_plugin(
        tenant_id="tenant1",
        mesh_session_id="mesh1",
        config={"direction": direction, "other": "value"},
    )
    assert isinstance(result, RegistrationResult)
    assert result.direction == direction
    assert result.soulkey.startswith("soulkey-")
