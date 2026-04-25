from pydantic import BaseModel
from typing import Optional

class VerdictRequest(BaseModel):
    verdict: str
    verifier_model: Optional[str] = None
    generator_model: Optional[str] = None
    action_class: Optional[str] = None
    evidence_sha256: Optional[str] = None
