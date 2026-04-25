import pytest
from aletheia.app.prompt.parser import PromptParser, ParseError

well_formed_cases = [
    "Some explanation here.\nDONE verify=PASS",
    "All good.\nDONE verify={FAIL}",
    "Result: something.\nDONE verify=UNCERTAIN",
]

malformed_cases = [
    "No sentinel line here",
    "DONE verify=",
    "DONE verify=UNKNOWN",
]

@pytest.mark.parametrize("output,expected", [
    (well_formed_cases[0], ("PASS", "Some explanation here.")),
    (well_formed_cases[1], ("FAIL", "All good.")),
    (well_formed_cases[2], ("UNCERTAIN", "Result: something.")),
])
def test_parser_success(output, expected):
    assert PromptParser.parse(output) == expected

@pytest.mark.parametrize("output", malformed_cases)
def test_parser_failure(output):
    with pytest.raises(ParseError):
        PromptParser.parse(output)
