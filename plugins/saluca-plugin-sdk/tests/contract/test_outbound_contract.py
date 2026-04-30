import pytest
from saluca_plugin_sdk.contract.outbound import register, discover, lifecycle, dispatch_outbound
from plugins.saluca_plugin_echo_outbound.saluca_plugin_echo_outbound.plugin import EchoOutboundPlugin
from .fixtures.missing_actions_plugin import MissingActionsPlugin
import requests
from unittest import mock

@pytest.fixture
def mock_endpoint():
    with mock.patch("requests.post") as m:
        m.return_value.status_code = 200
        m.return_value.json.return_value = {"status": "received"}
        yield m

def test_outbound_contract_success(mock_endpoint):
    plugin = EchoOutboundPlugin()
    register(plugin)
    discover(plugin)
    lifecycle(plugin)
    result = dispatch_outbound(plugin, "echo.send", {"msg": "hello"})
    assert isinstance(result, DispatchResult)
    assert result.output == {"status": "received"}
    mock_endpoint.assert_called_once()

def test_missing_outbound_actions_fails():
    plugin = MissingActionsPlugin()
    with pytest.raises(AssertionError):
        register(plugin)
