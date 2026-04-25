# ALT-03: Verify prompt template + sentinel parser

## Target paths
- aletheia/app/prompt/__init__.py
- aletheia/app/prompt/parser.py
- aletheia/prompts/verify_v1.md
- aletheia/tests/test_parser.py

## Acceptance criteria
- APE/V: Parser against 20 canned outputs (10 well-formed, 10 malformed). All 10 well-formed → correct `(verdict, rationale)`, all 10 malformed → `ParseError`. `pytest tests/test_parser.py` green. Prompt file sha256 pinned in env.

## Verification approach
- Unit tests in `aletheia/tests/test_parser.py` exercise both well‑formed and malformed cases.
- CI runs `pytest` on the `aletheia` package; the test suite must pass.

## Risks
- Incorrect sentinel detection could cause false PASS/FAIL results.
- Regex may mis‑interpret multi‑line rationale containing the word “DONE”.
