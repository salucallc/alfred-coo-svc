from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class AgentCapabilities:
    supported_tasks: List[str]
    max_concurrent: int = 1


@dataclass
class RegistrationResult:
    plugin_id: str
    timestamp: datetime
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class DispatchResult:
    success: bool
    result: Optional[Any] = None
    error: Optional[Exception] = None


@dataclass
class AuditEvent:
    event_type: str
    payload: Dict[str, Any]
    timestamp: datetime = datetime.utcnow()
