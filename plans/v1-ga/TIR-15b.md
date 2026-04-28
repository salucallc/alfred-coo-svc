# TIR-15B: QA review + documentation for Tiresias epic

## Target paths
- docs/qa-reviews/tir-phase-b-review.md (new)
- plans/v1-ga/TIR-15b.md (new)

## Acceptance criteria

- Enumerate every PR with the `tiresias` label or title starting with `[TIR]` opened in v1-GA window
- Run constrained hawkman-qa-a review per PR (2 tool-call budget, ≤300 char rationale, correctness/scope/docs axes)
- Aggregate into markdown table in `docs/qa-reviews/tir-phase-b-review.md`
- Each row: PR number, title, verdict {PASS,FAIL,CONCERN}, rationale ≤300 chars
- Include model id and timestamp in report header
- Row count matches PR enumeration count

## Verification approach

Manual verification: check file exists at target path; count rows matches TIR PR count (15); verify each verdict is PASS/FAIL/CONCERN; spot-check rationale length ≤300 chars; confirm header includes model id and timestamp.

## Risks

- Missed PRs in enumeration: mitigated by using `gh pr list --label tiresias` and title pattern matching
- Rationale exceeds 300 chars: mitigated by constrained prompt and manual truncation if needed
- Model quota exhaustion: using `hf:openai/gpt-oss-120b:fastest` which is free tier