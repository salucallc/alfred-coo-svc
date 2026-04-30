import pytest
from saluca_plugin_sdk import SalucaPlugin

class InboundPlugin(SalucaPlugin):
    direction = "inbound"

    def discover(self): pass
    def register(self): pass
    def lifecycle(self): pass
    def audit_hook(self, event): pass
    def unregister(self): pass
    # missing dispatch_inbound override

class OutboundPlugin(SalucaPlugin):
    direction = "outbound"

    def discover(self): pass
    def register(self): pass
    def lifecycle(self): pass
    def audit_hook(self, event): pass
    def unregister(self): pass
    def dispatch_outbound(self, agent_id, action, scope):
        return True

def test_inbound_missing_override():
    plugin = InboundPlugin()
    with pytest.raises(NotImplementedError):
        plugin.dispatch_inbound("id", {}, "scope")

def test_outbound_override():
    plugin = OutboundPlugin()
    assert plugin.dispatch_outbound("id", {}, "scope") is True
