# ALT-02: Verdict data model + soul-svc writer

## Target paths
- aletheia/app/main.py
- aletheia/app/verdict.py
- aletheia/app/soul_writer.py
- aletheia/tests/test_verdict_writer.py

## Acceptance criteria
- Insert synthetic verdict via `POST /v1/_debug/verdict`; verify `mcp__alfred__soul_memory_search` with `topic=aletheia.verdict` returns record with `{verdict, verifier_model, generator_model, action_class, evidence_sha256, created_at}`. JSON-schema validated in CI.

## Verification approach
- Unit test `test_verdict_writer.py` posts a payload and asserts the response contains the recorded verdict.

## Risks
- The current `write_verdict` is a stub; integration with the real soul-svc will need implementation before production use.
