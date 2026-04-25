from .verdict import Verdict

def write_verdict(verdict: Verdict) -> dict:
    """Write a verdict to the soul‑svc.

    In the real system this would POST to ``/v1/_debug/verdict`` and
    persist the evidence bundle. Here we return a minimal payload
    suitable for unit‑testing.
    """
    return {
        "verdict": verdict.verdict,
        "reason": verdict.reason,
        "topic": "aletheia.verdict",
    }
