"""Behavioral APE/V guardrail — Hawkman QA hard gate against plan-only PRs.

The 2026-04-29 follow-up to memory record `feedback_apev_must_be_behavioral`:

> Hawkman QA passes on syntactic 'section renders' but misses 'section reflects
> reality'; APE/V must include end-to-end data-contract assertion, not just
> structural check.

The existing pre-merge stack — pr-body-apev-lint workflow, hawkman GATE 1
(verbatim citation), GATE 4 (per-criterion evidence) — is all *structural*:
it proves the PR body quotes the criteria and labels each one. It does NOT
prove the diff actually implements anything. Today's open queue (20 PRs as
of 2026-04-29 session) is dominated by `AB-22: Add plan doc for X` PRs that
ship a single .md file and pass all current gates.

This module is the shared helper for three new behavioral gates that run
post-verdict (Layer 2, in `_handle_review_verdict`) and pre-merge (Layer 3,
in `_merge_pr`):

Gate B1 — Code-vs-plan check.
    Reject if the PR diff is dominated by .md/.txt plan docs with
    <10% non-doc lines changed AND no test added.
    Verdict reason: ``plan_only_no_implementation``.

Gate B2 — Test-coverage assertion.
    Require either a new test file OR a modified test file that
    references at least one symbol from the new/changed source files.
    If neither, REQUEST_CHANGES.
    Verdict reason: ``tests_dont_cover_changes``.

Gate B3 — Data-contract assertion.
    For each API/CLI/mesh/persona surface change in the diff
    (FastAPI route, CLI command, mesh task type, persona contract),
    require at least one test in the diff that imports from the
    same module. If none, REQUEST_CHANGES.
    Verdict reason: ``surface_change_lacks_e2e_test``.

These three gates supplement (do not replace) the existing structural
gates. A PR must pass BOTH structural and behavioral checks to APPROVE.

Design notes:
- Pure-Python, no I/O. Tests inject the diff as a list of dicts; the
  orchestrator wires it up via the same ``pr_files_get`` shape used by
  destructive_guardrail.
- Heuristics are intentionally conservative (ratio thresholds tuned so
  doc-heavy-but-real PRs still pass). Tune via constants below.
- First-trip wins: B1 → B2 → B3, in declared order.
- Fail-open on missing data: if pr_files is None or empty, return
  non-tripped. The orchestrator never blocks on flaky GitHub API.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("alfred_coo.autonomous_build.behavioral_apev")


# Tunable thresholds.

# Gate B1: ratio of non-doc lines (additions + changes) to total lines
# changed. Below this fraction AND no test added → tripped.
NON_DOC_LINE_FRACTION_FLOOR = 0.10

# File extensions that count as "doc" (not implementation).
DOC_EXTENSIONS = (".md", ".txt", ".rst", ".adoc")

# Path prefixes that count as "doc-only" regardless of extension.
DOC_PATH_PREFIXES = ("docs/", "plans/")

# Minimum total LOC churn before B1 even runs. Tiny PRs (single-line
# typo fixes, version bumps) shouldn't trip "plan-only" because the
# ratio is arithmetically unstable at very low N.
B1_MIN_TOTAL_LOC = 5

# Test-file detection. A file is a test if its path contains any of
# these segments. Mirrored from the conventions used in this repo and
# python-test community standard.
TEST_PATH_HINTS = ("/tests/", "tests/", "/test_", "_test.py")

# Surface-change detection patterns. Each pattern tries to identify a
# new public API/CLI/mesh/persona surface in the diff content.
# Matches against added (+) lines only.
_SURFACE_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    # FastAPI / Flask / Starlette route decorators.
    ("fastapi_route", re.compile(
        r"@(?:router|app|api)\.(?:get|post|put|patch|delete|head|options|websocket)\s*\("
    )),
    # Typer / click CLI commands.
    ("cli_command", re.compile(
        r"@(?:app|cli|command|group)\.(?:command|callback|group)\s*\("
    )),
    # Mesh task intent / persona dispatch — string literal in a known shape.
    ("mesh_task_intent", re.compile(
        r"(?:intent|task_type|persona)\s*[:=]\s*[\"']\[?persona:[\w-]+"
    )),
    # New persona definition entry (this repo's persona.py shape).
    ("persona_def", re.compile(r"^\s*\"[\w-]+\":\s*Persona\s*\(")),
    # New BUILTIN_TOOLS entry (alfred_coo.tools shape).
    ("builtin_tool", re.compile(r"^\s*\"[\w_]+\":\s*ToolSpec\s*\(")),
)


@dataclass
class BehavioralGuardrailResult:
    """Outcome of running the three behavioral gates against a PR diff.

    Mirrors GuardrailResult from destructive_guardrail to keep the
    orchestrator wiring shape-compatible. ``layer`` carries the gate
    name (``plan_only_no_implementation`` / ``tests_dont_cover_changes``
    / ``surface_change_lacks_e2e_test``) for direct use as the
    verdict-override reason.
    """

    tripped: bool
    layer: str = ""
    reason: str = ""
    citations: List[str] = field(default_factory=list)


def _is_doc_file(path: str) -> bool:
    """A path is doc-only if it ends in a doc extension OR sits under a
    doc prefix.

    .github/ workflows are NOT docs (they're config). plans/ is doc-only
    by convention (planning markdown lives there).
    """
    if not path:
        return False
    lower = path.lower()
    if lower.endswith(DOC_EXTENSIONS):
        return True
    for prefix in DOC_PATH_PREFIXES:
        if lower.startswith(prefix) or f"/{prefix}" in lower:
            return True
    return False


def _is_test_file(path: str) -> bool:
    """A path is a test file if it sits under a tests/ tree or is named
    test_*.py / *_test.py / similar."""
    if not path:
        return False
    lower = path.lower()
    for hint in TEST_PATH_HINTS:
        if hint in lower:
            return True
    # File-basename forms.
    base = lower.rsplit("/", 1)[-1]
    if base.startswith("test_") and base.endswith(".py"):
        return True
    if base.endswith("_test.py"):
        return True
    return False


def _file_loc(f: Dict[str, Any]) -> Tuple[int, int]:
    """Extract (additions, deletions) ints from a pr_files entry."""
    try:
        adds = int(f.get("additions") or 0)
    except (TypeError, ValueError):
        adds = 0
    try:
        dels = int(f.get("deletions") or 0)
    except (TypeError, ValueError):
        dels = 0
    return adds, dels


def _file_path(f: Dict[str, Any]) -> str:
    return f.get("filename") or f.get("path") or ""


def _gate_b1_plan_only(
    pr_files: List[Dict[str, Any]],
) -> Optional[BehavioralGuardrailResult]:
    """Gate B1: plan-only PR detection.

    Reject if non-doc churn is < NON_DOC_LINE_FRACTION_FLOOR of total
    churn AND no test was added/modified.

    Returns a tripped result, or None if the gate passes.
    """
    total_loc = 0
    non_doc_loc = 0
    test_loc = 0
    doc_paths: List[str] = []
    code_paths: List[str] = []

    for f in pr_files:
        if not isinstance(f, dict):
            continue
        path = _file_path(f)
        if not path:
            continue
        adds, dels = _file_loc(f)
        churn = adds + dels
        if churn <= 0:
            continue
        total_loc += churn
        is_doc = _is_doc_file(path)
        is_test = _is_test_file(path)
        if is_test:
            test_loc += churn
            non_doc_loc += churn  # tests count as non-doc.
        elif not is_doc:
            non_doc_loc += churn
            code_paths.append(path)
        if is_doc:
            doc_paths.append(path)

    if total_loc < B1_MIN_TOTAL_LOC:
        return None  # too small to judge ratio.

    fraction = non_doc_loc / total_loc if total_loc > 0 else 0.0
    if fraction >= NON_DOC_LINE_FRACTION_FLOOR:
        return None  # enough real code/tests, gate passes.

    if test_loc > 0:
        return None  # has test churn, gate passes.

    citation = (
        f"non-doc fraction {fraction:.2%} < {NON_DOC_LINE_FRACTION_FLOOR:.0%} "
        f"floor; total_loc={total_loc}, non_doc_loc={non_doc_loc}, "
        f"test_loc={test_loc}; doc files: "
        f"{', '.join(doc_paths[:5])}{'...' if len(doc_paths) > 5 else ''}"
    )
    return BehavioralGuardrailResult(
        tripped=True,
        layer="plan_only_no_implementation",
        reason=(
            f"plan-only PR: {len(doc_paths)} doc file(s), "
            f"{len(code_paths)} non-doc file(s), "
            f"non-doc churn {non_doc_loc}/{total_loc} "
            f"({fraction:.2%}) under {NON_DOC_LINE_FRACTION_FLOOR:.0%} floor "
            f"and no test churn. Ship implementation + tests, not just docs."
        ),
        citations=[citation],
    )


# Identifier extraction. We grep added (+) and context lines for module-level
# defs / classes / assignments so a test that imports them can be matched.
_DEF_PATTERNS = (
    re.compile(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    re.compile(r"^\s*async\s+def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\s*[\(:]"),
    re.compile(r"^\s*([A-Z][A-Z0-9_]+)\s*=\s*"),  # CONST = ...
)


def _extract_added_symbols(patch: str) -> List[str]:
    """Pull def/class/CONST names from added (+) lines of a unified diff
    patch string.

    pr_files entries from GitHub include a `patch` field containing the
    raw diff. We scan only `+` lines (not `-`, not context) so we
    measure new public surface.

    Returns deduped list of symbol names. Empty on missing/malformed.
    """
    if not patch or not isinstance(patch, str):
        return []
    seen: Dict[str, None] = {}
    for line in patch.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:]  # strip the leading +
        for pat in _DEF_PATTERNS:
            m = pat.match(body)
            if m:
                seen[m.group(1)] = None
    return list(seen.keys())


def _module_name_from_path(path: str) -> str:
    """Convert ``src/alfred_coo/persona.py`` → ``alfred_coo.persona``.

    Strips the leading ``src/`` if present, drops the ``.py`` suffix,
    converts ``/`` to ``.``. Returns the basename without extension if
    the path doesn't match the expected layout.
    """
    if not path:
        return ""
    p = path
    if p.startswith("src/"):
        p = p[4:]
    if p.endswith(".py"):
        p = p[:-3]
    return p.replace("/", ".")


def _gate_b2_test_coverage(
    pr_files: List[Dict[str, Any]],
) -> Optional[BehavioralGuardrailResult]:
    """Gate B2: tests must touch the changed source.

    Pass if EITHER:
      a) at least one new (status='added') test file exists, OR
      b) at least one modified test file's diff references a symbol
         exported by a new/changed non-test source file, OR
      c) the PR has no non-test, non-doc source changes at all
         (e.g. config-only PR — no source to cover).

    Otherwise REQUEST_CHANGES with reason ``tests_dont_cover_changes``.
    """
    src_files: List[Dict[str, Any]] = []
    test_files: List[Dict[str, Any]] = []
    src_symbols: List[Tuple[str, str]] = []  # (path, symbol)

    for f in pr_files:
        if not isinstance(f, dict):
            continue
        path = _file_path(f)
        if not path:
            continue
        adds, dels = _file_loc(f)
        if adds + dels <= 0:
            continue
        if _is_test_file(path):
            test_files.append(f)
            continue
        if _is_doc_file(path):
            continue
        src_files.append(f)
        # Pull added symbols from the patch for cross-reference.
        for sym in _extract_added_symbols(f.get("patch") or ""):
            src_symbols.append((path, sym))

    if not src_files:
        return None  # no source changes to cover; gate passes vacuously.

    if not test_files:
        return BehavioralGuardrailResult(
            tripped=True,
            layer="tests_dont_cover_changes",
            reason=(
                f"PR modifies {len(src_files)} source file(s) but adds/"
                f"modifies zero test files. Ship at least one test that "
                f"exercises the changed surface."
            ),
            citations=[
                f"source paths: "
                f"{', '.join(_file_path(f) for f in src_files[:5])}"
                f"{'...' if len(src_files) > 5 else ''}",
            ],
        )

    # New test files (status=added) automatically cover — assume the
    # author wrote them for the change.
    for tf in test_files:
        if (tf.get("status") or "").lower() == "added":
            return None

    # Modified test files: scan their patch text for any added source
    # symbol or any added source module name.
    src_modules = {_module_name_from_path(_file_path(f)) for f in src_files}
    src_modules.discard("")
    for tf in test_files:
        patch = tf.get("patch") or ""
        if not isinstance(patch, str):
            continue
        # Scan only `+` lines so we measure the test-update intent.
        added_blob_parts = []
        for line in patch.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added_blob_parts.append(line[1:])
        added_blob = "\n".join(added_blob_parts)
        if not added_blob:
            continue
        for mod in src_modules:
            if mod and mod in added_blob:
                return None
        for _src_path, sym in src_symbols:
            if sym and re.search(rf"\b{re.escape(sym)}\b", added_blob):
                return None

    # Tests exist but none touch the changed sources.
    return BehavioralGuardrailResult(
        tripped=True,
        layer="tests_dont_cover_changes",
        reason=(
            f"PR modifies {len(src_files)} source file(s) and "
            f"{len(test_files)} test file(s), but no test diff "
            f"references any changed-source symbol or module. Tests "
            f"must exercise the changed code, not unrelated paths."
        ),
        citations=[
            f"source modules: {', '.join(sorted(src_modules))[:200]}",
            f"test paths: "
            f"{', '.join(_file_path(f) for f in test_files[:5])}"
            f"{'...' if len(test_files) > 5 else ''}",
        ],
    )


def _detect_surface_changes(
    pr_files: List[Dict[str, Any]],
) -> List[Tuple[str, str, str]]:
    """Return list of (path, surface_kind, sample_line) for files that
    add a public API/CLI/mesh/persona surface in their diff."""
    found: List[Tuple[str, str, str]] = []
    for f in pr_files:
        if not isinstance(f, dict):
            continue
        path = _file_path(f)
        if not path or _is_test_file(path) or _is_doc_file(path):
            continue
        patch = f.get("patch") or ""
        if not isinstance(patch, str):
            continue
        for line in patch.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            body = line[1:]
            for kind, pat in _SURFACE_PATTERNS:
                if pat.search(body):
                    found.append((path, kind, body.strip()[:120]))
                    break
    return found


def _gate_b3_surface_e2e(
    pr_files: List[Dict[str, Any]],
) -> Optional[BehavioralGuardrailResult]:
    """Gate B3: surface changes need an e2e test.

    For each new API/CLI/mesh/persona surface in the diff, look for a
    test file (added or modified) whose patch references the same
    module. If any surface has zero test coverage, trip.
    """
    surfaces = _detect_surface_changes(pr_files)
    if not surfaces:
        return None  # no public-surface change; gate vacuously passes.

    test_files: List[Dict[str, Any]] = [
        f for f in pr_files
        if isinstance(f, dict) and _is_test_file(_file_path(f))
    ]
    if not test_files:
        sample = "; ".join(
            f"{p} → {kind}" for p, kind, _l in surfaces[:5]
        )
        return BehavioralGuardrailResult(
            tripped=True,
            layer="surface_change_lacks_e2e_test",
            reason=(
                f"PR adds {len(surfaces)} public surface(s) "
                f"({', '.join({k for _p, k, _l in surfaces})}) but "
                f"includes no test file. Each new surface needs at "
                f"least one test that exercises a real input → output."
            ),
            citations=[f"surfaces: {sample}"],
        )

    # Build the union of added test diffs to scan for module references.
    test_blobs: List[str] = []
    for tf in test_files:
        patch = tf.get("patch") or ""
        if isinstance(patch, str):
            for line in patch.splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    test_blobs.append(line[1:])
    test_blob = "\n".join(test_blobs)

    uncovered: List[Tuple[str, str, str]] = []
    for path, kind, sample_line in surfaces:
        mod = _module_name_from_path(path)
        if mod and mod in test_blob:
            continue
        # Also accept the basename of the file (for non-py surfaces).
        base = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if base and re.search(rf"\b{re.escape(base)}\b", test_blob):
            continue
        uncovered.append((path, kind, sample_line))

    if not uncovered:
        return None  # every surface has at least one test reference.

    citation = "; ".join(
        f"{p} → {kind}: `{line}`" for p, kind, line in uncovered[:5]
    )
    return BehavioralGuardrailResult(
        tripped=True,
        layer="surface_change_lacks_e2e_test",
        reason=(
            f"{len(uncovered)} new surface(s) lack a test that "
            f"references the surface's module. Each surface "
            f"({', '.join({k for _p, k, _l in uncovered})}) needs an "
            f"end-to-end test exercising a real input → real output."
        ),
        citations=[citation],
    )


def compute_behavioral_apev(
    pr_files: list,
) -> BehavioralGuardrailResult:
    """Run B1 → B2 → B3 against a PR's file list.

    Returns first-trip result, or non-tripped if all three gates pass.
    Mirrors compute_destructive_guardrails' return shape so the
    orchestrator wiring is uniform.

    pr_files is the GitHub GET /repos/{owner}/{repo}/pulls/{N}/files
    list shape: each entry has filename, status, additions, deletions,
    and (optionally) patch. The pr_files_get tool wraps the same shape.

    Fail-open on missing data: if pr_files is None / empty / malformed,
    we return non-tripped. The orchestrator never blocks on an empty
    file list (would block all genuinely empty PRs).
    """
    if not isinstance(pr_files, list) or not pr_files:
        return BehavioralGuardrailResult(tripped=False)

    for gate in (_gate_b1_plan_only, _gate_b2_test_coverage, _gate_b3_surface_e2e):
        result = gate(pr_files)
        if result is not None:
            return result

    return BehavioralGuardrailResult(tripped=False)


__all__ = [
    "BehavioralGuardrailResult",
    "compute_behavioral_apev",
    "NON_DOC_LINE_FRACTION_FLOOR",
    "DOC_EXTENSIONS",
    "DOC_PATH_PREFIXES",
    "TEST_PATH_HINTS",
    "B1_MIN_TOTAL_LOC",
]
