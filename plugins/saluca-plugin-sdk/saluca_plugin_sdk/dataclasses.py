from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict

@dataclass
class AgentCapabilities:
    supports_inbound: bool
    supports_outbound: bool

@dataclass
class RegistrationResult:
    agent_id: str
    capabilities: AgentCapabilities
    metadata: Dict[str, Any] | None = None

@dataclass
class DispatchResult:
    success: bool
    details: Dict[str, Any] | None = None

@dataclass
class AuditEvent:
    event_type: str
    payload: Dict[str, Any]
    timestamp: str
