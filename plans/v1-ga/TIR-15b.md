# TIR-15B: Phase-B QA Review Aggregation

## Target paths
- docs/qa-reviews/tir-phase-b-review.md
- plans/v1-ga/TIR-15b.md

## Acceptance criteria
**A — Action:**
1. Enumerate every PR with the `tiresias` label or whose title starts with `[TIR]` ... (see APE/V section).
2. Run constrained hawkman-qa-a reviews per PR.
3. Aggregate results into markdown report.
4. Commit report at specified path.

**V — Verification (machine-checkable):**
1. Report file exists.
2. Row count matches PR list.
3. Verdicts are PASS/FAIL/CONCERN.
4. Rationale <= 300 chars.
5. Header includes model id and timestamp.
