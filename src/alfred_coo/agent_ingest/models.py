from pydantic import BaseModel, Field, validator
from typing import Any, Dict, Optional
from uuid import UUID

class ExternalAgent(BaseModel):
    agent_id: UUID = Field(default_factory=UUID)  # DB generated
    tenant_id: UUID
    plugin_id: str
    plugin_version: str
    direction: str
    framework_or_surface: str
    framework_or_surface_version: str
    mode: str
    manifest_only: bool = False
    capabilities: Dict[str, Any]
    actions: Optional[Dict[str, Any]] = None
    scope: Dict[str, Any]
    policy: Dict[str, Any]
    soulkey_kid: Optional[str] = None
    harness: Optional[str] = None
    registered_at: Optional[str] = None
    last_health_check: Optional[str] = None
    status: str = "active"

    @validator("direction")
    def direction_allowed(cls, v):
        if v not in {"inbound", "outbound", "bidirectional"}:
            raise ValueError("invalid direction")
        return v

    @validator("status")
    def status_allowed(cls, v):
        if v not in {"active", "paused", "error", "revoked"}:
            raise ValueError("invalid status")
        return v
