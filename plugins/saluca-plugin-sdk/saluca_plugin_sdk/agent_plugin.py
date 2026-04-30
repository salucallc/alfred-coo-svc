import abc
from typing import Any, Dict

class SalucaPlugin(abc.ABC):
    """
    Base class for Saluca plugins.
    Subclasses must set `direction` to one of "inbound", "outbound", "bidirectional".
    Depending on the direction they must implement the corresponding dispatch methods.
    """

    direction: str

    @abc.abstractmethod
    def discover(self) -> Any:
        ...

    @abc.abstractmethod
    def register(self, *args, **kwargs) -> Any:
        ...

    @abc.abstractmethod
    def lifecycle(self, *args, **kwargs) -> Any:
        ...

    @abc.abstractmethod
    def audit_hook(self, event: "AuditEvent") -> Any:
        ...

    @abc.abstractmethod
    def unregister(self) -> Any:
        ...

    def dispatch_inbound(self, agent_id: str, task: Dict[str, Any], scope: str) -> Any:
        raise NotImplementedError("Inbound dispatch not implemented for this plugin")

    def dispatch_outbound(self, agent_id: str, action: Dict[str, Any], scope: str) -> Any:
        raise NotImplementedError("Outbound dispatch not implemented for this plugin")

# Legacy alias
AgentPlugin = SalucaPlugin
