import re

class ParseError(Exception):
    pass

def parse_verification_output(text: str):
    """Parse the verifier's output looking for a sentinel line.

    Expected format: ``DONE verify={PASS|FAIL|UNCERTAIN}`` possibly preceded by whitespace.
    Returns the status string ('PASS', 'FAIL', or 'UNCERTAIN').
    Raises ParseError if the sentinel is missing or malformed.
    """
    pattern = r"DONE\s+verify=\{?(PASS|FAIL|UNCERTAIN)\}?"
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        raise ParseError("Sentinel line not found or malformed")
    return match.group(1).upper()
