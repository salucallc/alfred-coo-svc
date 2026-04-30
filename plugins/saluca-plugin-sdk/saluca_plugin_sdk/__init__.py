"""
Saluca Plugin SDK
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
