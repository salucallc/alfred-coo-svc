import abc
from typing import Any, Protocol
from .dataclasses import AgentCapabilities, RegistrationResult, DispatchResult, AuditEvent


class SalucaPlugin(abc.ABC):
    """Abstract base class for Saluca plugins.

    Subclasses must define a ``direction`` attribute with one of the values:
    ``"inbound"``, ``"outbound"`` or ``"bidirectional"``.
    Depending on the direction, the corresponding dispatch method must be overridden.
    """

    direction: str

    @abc.abstractmethod
    def discover(self) -> Any:
        """Discover available agents or resources."""
        pass

    @abc.abstractmethod
    def register(self, capabilities: AgentCapabilities) -> RegistrationResult:
        """Register the plugin with given capabilities."""
        pass

    @abc.abstractmethod
    def lifecycle(self, action: str) -> Any:
        """Start, stop or health‑check the plugin lifecycle."""
        pass

    @abc.abstractmethod
    def audit_hook(self, event: AuditEvent) -> Any:
        """Hook invoked for audit events."""
        pass

    @abc.abstractmethod
    def unregister(self) -> Any:
        """Unregister the plugin and clean up resources."""
        pass

    # Conditional dispatch methods – subclasses may implement as required
    def dispatch_inbound(self, agent_id: str, task: Any, scope: Any) -> DispatchResult:
        raise NotImplementedError("dispatch_inbound not implemented for this plugin")

    def dispatch_outbound(self, agent_id: str, action: Any, scope: Any) -> DispatchResult:
        raise NotImplementedError("dispatch_outbound not implemented for this plugin")


# Legacy alias for backward compatibility
AgentPlugin = SalucaPlugin
