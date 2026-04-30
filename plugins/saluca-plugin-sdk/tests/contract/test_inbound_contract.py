import pytest
from saluca_plugin_sdk.contract.inbound import *  # placeholder import
from saluca_plugin_echo_inbound.plugin import EchoInboundPlugin
from saluca_plugin_sdk import register_plugin, unregister_plugin

@pytest.fixture
def echo_plugin():
    p = EchoInboundPlugin()
    register_plugin(p)
    yield p
    unregister_plugin(p)

def test_inbound_contract_pass(echo_plugin):
    # Assuming the SDK test runner will invoke this test via -m saluca_plugin_contract
    task = {"type": "test", "payload": "data"}
    result = echo_plugin.dispatch_inbound(task)
    assert result.status == "ok"
    assert result.output == task

def test_inbound_contract_failure():
    # Load deliberately broken plugin fixture
    from plugins.saluca_plugin_sdk.tests.contract.fixtures.broken_inbound_plugin import BrokenPlugin
    register_plugin(BrokenPlugin())
    with pytest.raises(AssertionError):
        # The SDK runner would raise on contract violation
        pass
    unregister_plugin(BrokenPlugin())
