from pydantic import BaseModel, ValidationError, validator
from typing import Literal, List, Any


class ExternalAgent(BaseModel):
    kind: Literal["ExternalAgent"] = "ExternalAgent"
    agent_id: str
    capabilities: List[str]
    scope: str

    @validator("scope")
    def check_scope(cls, v: str) -> str:
        if v not in {"global", "local"}:
            raise ValueError("invalid scope enum")
        return v


class ExternalSurface(BaseModel):
    kind: Literal["ExternalSurface"] = "ExternalSurface"
    surface_id: str
    description: str
    scope: str

    @validator("scope")
    def check_scope(cls, v: str) -> str:
        if v not in {"global", "local"}:
            raise ValueError("invalid scope enum")
        return v


def validate_manifest(manifest: Any) -> List[str]:
    """Validate a manifest dict.
    Returns a list of error messages; empty list means valid.
    """
    errors = []
    try:
        if manifest.get("kind") == "ExternalAgent":
            ExternalAgent(**manifest)
        elif manifest.get("kind") == "ExternalSurface":
            ExternalSurface(**manifest)
        else:
            errors.append("unknown kind")
    except ValidationError as ve:
        errors.extend([e['msg'] for e in ve.errors()])
    return errors
