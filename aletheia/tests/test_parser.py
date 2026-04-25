import pytest
from aletheia.app.prompt.parser import parse_output, ParseError

def test_parse_well_formed_pass():
    output = "Result: everything looks fine\nDONE verify=PASS"
    verdict, rationale = parse_output(output)
    assert verdict == "PASS"
    assert rationale == "Result: everything looks fine"

def test_parse_well_formed_fail():
    output = "Error: missing required field\nDONE verify=FAIL"
    verdict, rationale = parse_output(output)
    assert verdict == "FAIL"
    assert rationale == "Error: missing required field"

def test_parse_uncertain():
    output = "Could not find evidence for sentinel\nDONE verify=UNCERTAIN"
    verdict, rationale = parse_output(output)
    assert verdict == "UNCERTAIN"
    assert rationale == "Could not find evidence for sentinel"

def test_missing_sentinel_raises():
    output = "Just some output without sentinel"
    with pytest.raises(ParseError):
        parse_output(output)

def test_invalid_verdict_raises():
    output = "All good\nDONE verify=UNKNOWN"
    with pytest.raises(ParseError):
        parse_output(output)
