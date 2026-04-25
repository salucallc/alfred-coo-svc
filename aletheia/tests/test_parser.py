import pytest
from aletheia.app.prompt.parser import parse_verify_output, ParseError

def test_parse_well_formed_pass():
    output = "All checks passed.\nDONE verify=PASS"
    verdict, rationale = parse_verify_output(output)
    assert verdict == "PASS"
    assert rationale == "All checks passed."

def test_parse_well_formed_fail():
    output = "Found an error.\nDONE verify=FAIL"
    verdict, rationale = parse_verify_output(output)
    assert verdict == "FAIL"
    assert rationale == "Found an error."

def test_parse_malformed_missing_sentinel():
    output = "Everything looks good."
    with pytest.raises(ParseError):
        parse_verify_output(output)

def test_parse_malformed_bad_sentinel():
    output = "All good.\nDONE verify=OK"
    with pytest.raises(ParseError):
        parse_verify_output(output)
