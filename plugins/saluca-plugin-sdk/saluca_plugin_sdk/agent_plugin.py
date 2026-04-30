from abc import ABC, abstractmethod
from typing import Any

class SalucaPlugin(ABC):
    direction: str

    @abstractmethod
    def discover(self) -> Any:
        ...

    @abstractmethod
    def register(self, *args, **kwargs) -> Any:
        ...

    @abstractmethod
    def lifecycle(self, action: str) -> Any:
        ...

    @abstractmethod
    def audit_hook(self, event: Any) -> Any:
        ...

    @abstractmethod
    def unregister(self) -> Any:
        ...

    # Optional methods overridden based on direction
    def dispatch_inbound(self, agent_id: str, task: Any, scope: Any) -> Any:
        raise NotImplementedError("dispatch_inbound not implemented")

    def dispatch_outbound(self, agent_id: str, action: Any, scope: Any) -> Any:
        raise NotImplementedError("dispatch_outbound not implemented")

# Legacy alias
AgentPlugin = SalucaPlugin
