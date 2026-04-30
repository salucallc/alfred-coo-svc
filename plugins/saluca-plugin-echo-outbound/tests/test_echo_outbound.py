import pytest
from plugins.saluca_plugin_echo_outbound.saluca_plugin_echo_outbound.plugin import EchoOutboundPlugin
from unittest import mock

@pytest.fixture
def mock_endpoint():
    with mock.patch("requests.post") as m:
        m.return_value.status_code = 200
        m.return_value.json.return_value = {"echo": "ok"}
        yield m

def test_dispatch_outbound_success(mock_endpoint):
    plugin = EchoOutboundPlugin()
    plugin.config = {"endpoint": "http://example.com/echo"}
    result = plugin.dispatch_outbound("echo.send", {"data": 123})
    assert result.output == {"echo": "ok"}
    mock_endpoint.assert_called_once_with("http://example.com/echo", json={"data": 123})
