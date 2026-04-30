from pydantic import BaseModel, Field
from typing import Literal

class ExternalAgent(BaseModel):
    name: str
    scope: Literal["public", "private"]
    version: str

class ExternalSurface(BaseModel):
    name: str
    scope: Literal["public", "private"]
    description: str

def validate_external_agent(data: dict) -> ExternalAgent:
    return ExternalAgent(**data)

def validate_external_surface(data: dict) -> ExternalSurface:
    return ExternalSurface(**data)
