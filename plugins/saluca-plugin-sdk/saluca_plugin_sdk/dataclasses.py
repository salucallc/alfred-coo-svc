from dataclasses import dataclass
from typing import Any, List

@dataclass
class AgentCapabilities:
    capabilities: List[str]

@dataclass
class RegistrationResult:
    success: bool
    details: Any = None

@dataclass
class DispatchResult:
    success: bool
    response: Any = None

@dataclass
class AuditEvent:
    event_type: str
    payload: dict
