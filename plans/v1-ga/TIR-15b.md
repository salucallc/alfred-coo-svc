# TIR-15b: Phase-B QA Review

## Target paths
- docs/qa-reviews/tir-phase-b-review.md

## Acceptance criteria
- File `docs/qa-reviews/tir-15b-<YYYY-MM-DD>.md` exists in the target repo.
- Report contains exactly one row per enumerated PR (count matches `gh pr list` count).
- Every row has a verdict in `{PASS, FAIL, CONCERN}`.
- Every row's rationale field length is 300 chars or fewer.
- Report header includes the model id (`hf:openai/gpt-oss-120b:fastest`) and the run timestamp.

## Verification approach
Create report at `docs/qa-reviews/tir-phase-b-review.md` meeting the verification criteria.

## Risks
- Date placeholder mismatch between expected filename pattern and created file.
