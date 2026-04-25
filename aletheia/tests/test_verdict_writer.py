import pytest
from aletheia.app.verdict import Verdict
from aletheia.app.soul_writer import write_verdict

def test_verdict_model_fields():
    v = Verdict(verdict="PASS", reason="All good")
    assert v.verdict == "PASS"
    assert v.reason == "All good"

def test_write_verdict_returns_expected_structure():
    v = Verdict(verdict="FAIL")
    payload = write_verdict(v)
    assert payload["verdict"] == "FAIL"
    assert payload["topic"] == "aletheia.verdict"
