import pytest
from saluca_plugin_sdk import SalucaPlugin, AgentCapabilities, RegistrationResult, DispatchResult, AuditEvent
from saluca_plugin_sdk.agent_plugin import SalucaPlugin as BasePlugin

class InboundMissingDispatch(SalucaPlugin):
    direction = "inbound"
    def discover(self):
        pass
    def register(self, capabilities: AgentCapabilities) -> RegistrationResult:
        return RegistrationResult(plugin_id="id", timestamp=__import__("datetime").datetime.utcnow())
    def lifecycle(self, action: str):
        pass
    def audit_hook(self, event: AuditEvent):
        pass
    # No dispatch_inbound override – should fail CI conformance

class OutboundWithDispatch(SalucaPlugin):
    direction = "outbound"
    def discover(self):
        pass
    def register(self, capabilities: AgentCapabilities) -> RegistrationResult:
        return RegistrationResult(plugin_id="id", timestamp=__import__("datetime").datetime.utcnow())
    def lifecycle(self, action: str):
        pass
    def audit_hook(self, event: AuditEvent):
        pass
    def dispatch_outbound(self, agent_id: str, action, scope) -> DispatchResult:
        return DispatchResult(success=True, result="ok")


def test_inbound_missing_override_fails():
    plugin = InboundMissingDispatch()
    with pytest.raises(NotImplementedError):
        plugin.dispatch_inbound("aid", {}, {})


def test_outbound_override_passes():
    plugin = OutboundWithDispatch()
    result = plugin.dispatch_outbound("aid", "act", {})
    assert result.success
