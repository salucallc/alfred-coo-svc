import re
from typing import Tuple, Optional

class ParseError(Exception):
    """Raised when the parser cannot extract a verdict from the output."""
    pass

class PromptParser:
    """Parse verifier output for the sentinel line.

    Expected sentinel format:
        DONE verify={PASS|FAIL|UNCERTAIN}
    """

    SENTINEL_RE = re.compile(r"DONE\s+verify=\{?(PASS|FAIL|UNCERTAIN)\}?")

    @classmethod
    def parse(cls, output: str) -> Tuple[str, Optional[str]]:
        """Return (verdict, rationale) if found, else raise ParseError.

        The rationale is the text preceding the sentinel line, stripped.
        """
        match = cls.SENTINEL_RE.search(output)
        if not match:
            raise ParseError("Sentinel line not found in output")
        verdict = match.group(1)
        rationale = output[:match.start()].strip() or None
        return verdict, rationale
