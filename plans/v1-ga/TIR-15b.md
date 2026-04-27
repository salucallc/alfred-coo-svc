# SAL-3069: TIR-15B Phase-B QA Review

## Target paths
- docs/qa-reviews/tir-phase-b-review.md
- plans/v1-ga/TIR-15b.md

## Acceptance criteria
**A — Action:**

1. Enumerate every PR with the `tiresias` label or whose title starts with `[TIR]` that was opened against any TIR-related repo within the v1-GA window. Source the list via `gh pr list --label tiresias --state all --json number,title,url,repository --limit 100`.
2. For each PR, run a constrained `hawkman-qa-a` (model: `hf:openai/gpt-oss-120b:fastest`) review with a strict prompt template: 2 tool-call budget, body output per PR no more than 300 chars, three axes (correctness, scope-discipline, docs-coverage).
3. Aggregate results into `Z:/_evidence/tir-15b-qa-review-<YYYY-MM-DD>.md` with one row per PR (PR num, title, verdict PASS/FAIL/CONCERN, rationale within 300 chars).
4. Commit the aggregated report into the appliance repo at `docs/qa-reviews/tir-15b-<YYYY-MM-DD>.md`.

**P — Plan:**

* Use `gh pr list ... --json` for the enumeration.
* Drive each per-PR review via `python C:/Users/cris/.claude/tools/ollama_sub.py --model hf:openai/gpt-oss-120b:fastest --system "<constrained-QA-prompt>" --prompt "<PR diff + meta>"`.
* Constrain the system prompt to the proven 2-tool-call + 300-char body budget.
* Roll up into a single markdown table.

**E — Evidence:**

* Output of `gh pr list ...` captured to `Z:/_evidence/tir-15b-pr-list.json`.
* Per-PR raw QA outputs captured under `Z:/_evidence/tir-15b/`.
* Final aggregated report committed to repo at the path above.

**V — Verification (machine-checkable):**

1. File `docs/qa-reviews/tir-15b-<YYYY-MM-DD>.md` exists in the target repo.
2. Report contains exactly one row per enumerated PR (count matches `gh pr list` count).
3. Every row has a verdict in `{PASS, FAIL, CONCERN}`.
4. Every row's rationale field length is 300 chars or fewer.
5. Report header includes the model id (`hf:openai/gpt-oss-120b:fastest`) and the run timestamp.

## Verification
File existence and format will be validated by CI and reviewer.
