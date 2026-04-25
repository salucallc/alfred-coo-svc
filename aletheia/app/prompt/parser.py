import re
from typing import Tuple

def parse_verdict(output: str) -> Tuple[str, str]:
    """
    Parse the verifier output and return a (verdict, rationale) tuple.

    The output must end with a sentinel line:
        DONE verify={PASS|FAIL|UNCERTAIN}
    Anything before the sentinel is treated as rationale.
    """
    sentinel_match = re.search(r"DONE verify=(PASS|FAIL|UNCERTAIN)", output)
    if not sentinel_match:
        raise ValueError("Missing DONE verify sentinel")
    verdict = sentinel_match.group(1)
    rationale = output[:sentinel_match.start()].strip()
    return verdict, rationale
