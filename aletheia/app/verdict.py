from pydantic import BaseModel

class Verdict(BaseModel):
    verdict: str
    verifier_model: str
    generator_model: str
    action_class: str
    evidence_sha256: str
    created_at: str

def create_verdict(**kwargs) -> Verdict:
    """Factory helper to create a Verdict instance from keyword args."""
    return Verdict(**kwargs)
