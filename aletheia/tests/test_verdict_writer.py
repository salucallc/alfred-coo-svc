import pytest
from aletheia.app.verdict import Verdict, create_verdict
from aletheia.app.soul_writer import write_verdict

def test_verdict_creation():
    v = create_verdict(
        verdict="PASS",
        verifier_model="model1",
        generator_model="model2",
        action_class="test_action",
        evidence_sha256="abc123",
        created_at="2026-04-25T00:00:00Z",
    )
    assert isinstance(v, Verdict)
    assert v.verdict == "PASS"

def test_soul_writer():
    data = {"verdict": "PASS"}
    assert write_verdict(data) is True
