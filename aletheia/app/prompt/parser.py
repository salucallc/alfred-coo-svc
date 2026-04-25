class ParseError(Exception):
    """Raised when verifier output cannot be parsed correctly."""


def parse_output(output: str) -> tuple[str, str]:
    """Parse verifier output.

    Returns a tuple (verdict, rationale). The last line must be of the form
    ``DONE verify=<VERDICT>`` where <VERDICT> is PASS, FAIL, or UNCERTAIN.
    Everything before the sentinel line is returned as the rationale.
    """
    if not isinstance(output, str):
        raise ParseError("Output must be a string")
    lines = [line.rstrip() for line in output.strip().splitlines() if line.strip()]
    if not lines:
        raise ParseError("Empty output")
    sentinel = lines[-1]
    if not sentinel.startswith("DONE verify="):
        raise ParseError("Missing sentinel line starting with 'DONE verify='")
    verdict = sentinel.split("=", 1)[1].strip()
    if verdict not in {"PASS", "FAIL", "UNCERTAIN"}:
        raise ParseError(f"Invalid verdict '{verdict}'")
    rationale = "\n".join(lines[:-1])
    return verdict, rationale

__all__ = ["ParseError", "parse_output"]
