from pydantic import BaseModel

class Verdict(BaseModel):
    """Data model for a verification verdict.

    Attributes:
        verdict: The verdict result, e.g. "PASS", "FAIL", or "UNCERTAIN".
        reason: Optional human‑readable explanation.
    """

    verdict: str
    reason: str | None = None
