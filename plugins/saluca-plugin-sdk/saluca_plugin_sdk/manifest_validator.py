from pydantic import BaseModel, ValidationError, Field
from typing import Literal, List, Dict, Any

class ExternalAgent(BaseModel):
    kind: Literal["ExternalAgent"]
    agent_id: str
    capabilities: List[str]
    metadata: Dict[str, Any] = {}

class ExternalSurface(BaseModel):
    kind: Literal["ExternalSurface"]
    surface_id: str
    scope: str
    config: Dict[str, Any] = {}

def validate_external_agent(data: dict) -> ExternalAgent:
    return ExternalAgent(**data)

def validate_external_surface(data: dict) -> ExternalSurface:
    return ExternalSurface(**data)
