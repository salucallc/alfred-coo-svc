import pytest
from saluca_plugin_sdk import register_plugin, unregister_plugin, DispatchResult
from saluca_plugin_echo_inbound.plugin import EchoInboundPlugin

@pytest.fixture
def plugin():
    p = EchoInboundPlugin()
    register_plugin(p)
    yield p
    unregister_plugin(p)

def test_dispatch_inbound_echo(plugin):
    task = {"type": "test", "payload": "hello"}
    result = plugin.dispatch_inbound(task)
    assert isinstance(result, DispatchResult)
    assert result.status == "ok"
    assert result.output == task
