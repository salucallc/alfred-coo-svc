# TIR-15A: markdown-lint CI workflow + clean README/PRINCIPLES.md/runbook

**Parent:** TIR-15 (Tiresias docs hardening) — see `plans/v1-ga/TIR-15.md`
**Linear:** SAL-3068
**Wave:** 3

## Context

The TIR-15 parent's APE/V calls for "README + [PRINCIPLES.md](<http://PRINCIPLES.md>) + runbook lint clean; markdown-lint CI green" plus a constrained QA review on TIR PRs. The lint cleanup + CI workflow portion is fully mechanical and auto-buildable; the constrained QA review (subjective judgment about TIR PR quality) is the sibling TIR-15B. This ticket covers ONLY the lint-and-CI half.

## APE/V Acceptance

**A â€” Action:**

1. Add a markdown-lint GitHub Actions workflow at `.github/workflows/markdown-lint.yml` that runs `markdownlint-cli2` (or `markdownlint-cli`) over `README.md`, `PRINCIPLES.md`, and any file under `docs/runbook/` or matching `**/runbook*.md`.
2. Author / vendor a `.markdownlint.json` (or `.markdownlint-cli2.jsonc`) at the repo root with sensible defaults: line-length disabled, MD013 off, MD041 (first-line H1) on, MD024 (no-duplicate-heading) on with `siblings_only`.
3. Run the linter locally; fix every reported violation in `README.md`, `PRINCIPLES.md`, and the runbook(s). Do NOT rewrite content; only mechanical fixes (heading levels, list spacing, trailing whitespace, code-fence languages, etc.).
4. The CI workflow must run on `pull_request` and on `push` to `main`, and must fail the job on any lint error.

**P â€” Plan:**

* Inspect the target repo to enumerate the exact runbook files.
* Pick `markdownlint-cli2` (preferred â€” faster, supports `.markdownlint-cli2.jsonc`).
* Configure to ignore `node_modules/`, `.venv/`, `*/CHANGELOG.md`.
* Run `npx markdownlint-cli2 "**/*.md"` locally; iterate until clean.

**E â€” Evidence:**

* `git diff` showing the new workflow file, config file, and lint cleanup edits.
* Local `npx markdownlint-cli2 "README.md" "PRINCIPLES.md" "docs/runbook/**/*.md"` exits 0.
* CI run on the PR is green.

**V â€” Verification (machine-checkable):**

1. File `.github/workflows/markdown-lint.yml` exists.
2. File `.markdownlint.json` or `.markdownlint-cli2.jsonc` exists at repo root.
3. `npx markdownlint-cli2 "README.md" "PRINCIPLES.md"` exits 0.
4. The CI workflow YAML contains `on:` with both `pull_request` and `push` branches.
5. No file under the target list contains TAB indentation in code fences (mechanical proof of cleanup pass).
