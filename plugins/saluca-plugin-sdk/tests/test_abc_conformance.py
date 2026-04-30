import pytest
from saluca_plugin_sdk import SalucaPlugin

def plugin_conforms(cls):
    direction = getattr(cls, "direction", None)
    if direction in ("inbound", "bidirectional"):
        if cls.dispatch_inbound is SalucaPlugin.dispatch_inbound:
            raise AssertionError("dispatch_inbound not overridden")
    if direction in ("outbound", "bidirectional"):
        if cls.dispatch_outbound is SalucaPlugin.dispatch_outbound:
            raise AssertionError("dispatch_outbound not overridden")
    return True

def test_inbound_without_override_fails():
    class BadPlugin(SalucaPlugin):
        direction = "inbound"
        def discover(self): pass
        def register(self): pass
        def lifecycle(self, action): pass
        def audit_hook(self, event): pass
        def unregister(self): pass
    with pytest.raises(AssertionError):
        plugin_conforms(BadPlugin)

def test_inbound_with_override_passes():
    class GoodPlugin(SalucaPlugin):
        direction = "inbound"
        def discover(self): pass
        def register(self): pass
        def lifecycle(self, action): pass
        def audit_hook(self, event): pass
        def unregister(self): pass
        def dispatch_inbound(self, agent_id, task, scope):
            return None
    assert plugin_conforms(GoodPlugin)
