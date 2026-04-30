"""Top-level package for saluca-plugin-sdk.
Provides the SalucaPlugin abstract base class and related dataclasses.
"""

from .agent_plugin import SalucaPlugin, AgentPlugin
from .dataclasses import AgentCapabilities, RegistrationResult, DispatchResult, AuditEvent

__all__ = [
    "SalucaPlugin",
    "AgentPlugin",
    "AgentCapabilities",
    "RegistrationResult",
    "DispatchResult",
    "AuditEvent",
]
