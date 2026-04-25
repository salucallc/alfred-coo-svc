# Parser for Aletheia verifier output

"""Parser for Aletheia verifier output."""

import re

class ParseError(Exception):
    """Raised when the verifier output cannot be parsed."""

def parse_verify_output(output: str):
    """Parse the verifier's output.

    Expected format:
        ... any text ...
        DONE verify={PASS|FAIL|UNCERTAIN}

    Returns
        tuple[str, str]: (verdict, rationale)

    The rationale is the text preceding the ``DONE`` line,
    stripped of trailing whitespace.
    """
    if not isinstance(output, str):
        raise ParseError("Output must be a string")

    # Find the sentinel line
    sentinel_match = re.search(r"^DONE verify=(PASS|FAIL|UNCERTAIN)\s*$", output.strip(), re.MULTILINE)
    if not sentinel_match:
        raise ParseError("Missing or malformed DONE sentinel")

    verdict = sentinel_match.group(1)

    # Rationale is everything before the sentinel line
    parts = output.strip().rsplit("\n", 1)
    rationale = parts[0].strip() if len(parts) > 1 else ""

    return verdict, rationale
