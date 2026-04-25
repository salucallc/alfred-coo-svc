import pytest
from aletheia.app.prompt.parser import parse_verification_output, ParseError

well_formed = [
    "Some output... DONE verify={PASS}",
    "DONE verify=PASS",
    "Done verify={FAIL}",
    "DONE verify={UNCERTAIN}",
    "Random text\nDONE verify=FAIL",
]

malformed = [
    "No sentinel here",
    "DONE verify=",
    "DONE verify={}",
    "DONE verify=UNKNOWN",
    "DONE verify=PASS extra",
]

def test_well_formed():
    for out in well_formed:
        status = parse_verification_output(out)
        assert status in {"PASS", "FAIL", "UNCERTAIN"}

def test_malformed():
    for out in malformed:
        with pytest.raises(ParseError):
            parse_verification_output(out)
