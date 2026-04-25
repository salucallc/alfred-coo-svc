"""SAL-2869 - Destructive-PR guardrail.

Three-layer defense against the qwen3-coder:480b builder pattern of
deleting large chunks of existing code instead of doing the additive
work specified in the ticket hint. This module is the shared helper
imported by Layer 2 (hawkman verdict gate) and Layer 3 (pre-merge
static check). Layer 1 (the alfred-coo-a builder system prompt) is
text-only and lives in persona.py.

Background, 2026-04-25 destructive PR observations:

| PR # | Stats        | Note                                          |
| ---- | ------------ | --------------------------------------------- |
| #84  | +53/-204     | single-file 5KB->200B docker-compose nuke;    |
|      |              | auto-merged then reverted (broke main)        |
| #27  | +46/-2187    | multi-file router rewrite, caught + closed    |
| #21  | +215/-491    | refactor-styled mass delete                   |
| #65  | +81/-221     |                                               |
| #68  | +122/-204    |                                               |
| #69  | +23/-244     |                                               |
| #20  | +120/-520    |                                               |

Compared to PRs that are NOT destructive (must not trip):

| PR # | Stats        | Note                                          |
| ---- | ------------ | --------------------------------------------- |
| #25  | +91/-24      | SS-03 legitimate consolidation (ratio 0.79)   |
| #66  | +60/-58      | true 50/50 refactor                           |
| #87  | +204/-53     | revert PR, opposite ratio, never destructive  |

Two gates run in order; first-trip wins (per-file checked first).

Gate 1 (per-file, LOC-dependent):
    For each modified file, look up the ORIGINAL line count at base_ref
    (live via gh api repos/<o>/<r>/contents/<f>?ref=<base>) and
    derive a deletion threshold = min(0.7 * original_loc, 500). If
    file.deletions > threshold AND the hint description does NOT contain
    a deletion-license keyword (rewrite, replace, nuke, reset)
    scoped to the file path, trip.

Gate 2 (per-PR ratio):
    If total_deletions > 2 * total_additions AND total_deletions > 100
    AND the ticket carries no refactor label, trip.

Threshold values are first-pass; SAL-2869 ticket notes future tuning
goes in a follow-up. Hardcoded for now.
"""

from __future__ import annotations

import base64
import json as _json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger("alfred_coo.autonomous_build.destructive_guardrail")


# Tunable thresholds.
PER_FILE_DELETION_RATIO = 0.7
PER_FILE_DELETION_ABSOLUTE_CAP = 500

PER_PR_DELETIONS_TO_ADDITIONS_RATIO = 2
PER_PR_DELETIONS_FLOOR = 100

DELETION_LICENSE_KEYWORDS = ("rewrite", "replace", "nuke", "reset")

REFACTOR_LABEL = "refactor"


@dataclass
class GuardrailResult:
    """Outcome of running the two gates against a PR diff.

    layer is set to "per_file" or "per_pr" to identify which gate
    tripped. citations carries the file:metric or PR-level metric the
    caller should attach to the verdict body so the merge log is
    auditable.
    """

    tripped: bool
    layer: str = ""
    reason: str = ""
    citations: List[str] = field(default_factory=list)


def _hint_licenses_deletion(hint_description: str, file_path: str) -> bool:
    """Return True if the hint description contains a deletion keyword.

    Keyword check is case-insensitive and substring-based, we don't
    require the file path to literally appear because hints are
    typically short and ticket-scoped. False positives here only
    DISARM the gate (trade off: rare; hints rarely mention these
    keywords), so the bias toward leniency is acceptable.

    file_path is reserved for a future tighter scope (e.g.
    require the keyword + path to co-occur in one sentence) and is
    currently unused.
    """
    if not hint_description:
        return False
    lower = hint_description.lower()
    for kw in DELETION_LICENSE_KEYWORDS:
        if kw in lower:
            return True
    return False


def _has_refactor_label_lookup(labels: Optional[List[str]]) -> bool:
    """Case-insensitive check for the refactor label."""
    if not labels:
        return False
    return any(
        isinstance(lbl, str) and lbl.strip().lower() == REFACTOR_LABEL
        for lbl in labels
    )


def _fetch_original_file_loc(
    base_repo: str,
    base_ref: str,
    file_path: str,
    *,
    timeout: float = 10.0,
) -> Optional[int]:
    """Fetch line count for file_path at base_ref via GitHub API.

    Returns the integer line count, or None if the path is missing
    (404, file is brand-new, no prior LOC to delete) or if any
    transport / decoding error occurs (caller treats unknown as
    "use absolute cap only" per SAL-2869 first-trip semantics).

    base_repo must be "<owner>/<repo>". Uses GITHUB_TOKEN when set so
    private repos work; unauthenticated calls hit the 60 req/h
    public-IP rate limit.
    """
    if not base_repo or not file_path:
        return None
    url = (
        f"https://api.github.com/repos/{base_repo}/contents/"
        f"{urllib.request.quote(file_path)}?ref={urllib.request.quote(base_ref)}"
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "alfred-coo-svc/destructive_guardrail",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = _json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        logger.debug(
            "destructive_guardrail: gh contents %s@%s for %s -> http %s",
            base_repo, base_ref, file_path, e.code,
        )
        return None
    except Exception as e:  # noqa: BLE001 - best-effort only
        logger.debug(
            "destructive_guardrail: gh contents %s@%s for %s -> %s: %s",
            base_repo, base_ref, file_path, type(e).__name__, e,
        )
        return None

    if not isinstance(body, dict):
        return None
    encoding = body.get("encoding")
    raw = body.get("content") or ""
    if encoding == "base64":
        try:
            decoded = base64.b64decode(raw, validate=False)
        except Exception:
            return None
        try:
            text = decoded.decode("utf-8", errors="replace")
        except Exception:
            return None
    elif isinstance(raw, str):
        text = raw
    else:
        return None
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _per_file_deletion_threshold(original_loc: Optional[int]) -> int:
    """Compute the deletion threshold for a single file.

    When original_loc is None (lookup failed / brand-new file), fall
    back to the absolute cap so a brand-new file can never trip the
    per-file gate (deletions on a never-existed file = 0 anyway).
    """
    if original_loc is None or original_loc <= 0:
        return PER_FILE_DELETION_ABSOLUTE_CAP
    return min(
        int(PER_FILE_DELETION_RATIO * original_loc),
        PER_FILE_DELETION_ABSOLUTE_CAP,
    )


def compute_destructive_guardrails(
    pr_files: list,
    *,
    hint_description: str = "",
    has_refactor_label: bool = False,
    base_repo: str = "",
    base_ref: str = "main",
    original_loc_lookup=None,
) -> GuardrailResult:
    """Run the two destructive-PR gates against a PR's file list.

    pr_files is the GitHub GET /repos/{owner}/{repo}/pulls/{N}/files
    list shape: each entry has filename, status, additions, deletions.
    (The pr_files_get tool wraps the same shape with a path alias for
    filename, both are accepted.)

    Returns a GuardrailResult with tripped=True iff EITHER gate fails.
    First-trip wins (per-file checked first).

    original_loc_lookup is an optional callable (file_path: str) ->
    Optional[int] injected for test fixtures. When None (the default),
    the helper falls back to a live gh api contents call against
    base_repo @ base_ref.
    """
    if not isinstance(pr_files, list):
        return GuardrailResult(tripped=False)

    # Gate 1: per-file.
    for f in pr_files:
        if not isinstance(f, dict):
            continue
        path = f.get("filename") or f.get("path") or ""
        if not path:
            continue
        try:
            deletions = int(f.get("deletions") or 0)
        except (TypeError, ValueError):
            deletions = 0
        if deletions <= 0:
            continue

        if _hint_licenses_deletion(hint_description, path):
            continue

        if original_loc_lookup is not None:
            try:
                original_loc = original_loc_lookup(path)
            except Exception:
                original_loc = None
        else:
            original_loc = _fetch_original_file_loc(
                base_repo, base_ref, path
            )

        threshold = _per_file_deletion_threshold(original_loc)

        if deletions > threshold:
            citation = (
                f"{path}: deleted {deletions} lines "
                f"(threshold {threshold}; original_loc="
                f"{original_loc if original_loc is not None else 'unknown'})"
            )
            return GuardrailResult(
                tripped=True,
                layer="per_file",
                reason=(
                    f"per-file deletion gate tripped on {path}: "
                    f"{deletions} deletions exceeds threshold {threshold} "
                    f"(min(0.7 * "
                    f"{original_loc if original_loc is not None else 'unknown'}, "
                    f"500)); hint contains no rewrite/replace/nuke/reset "
                    f"keyword"
                ),
                citations=[citation],
            )

    # Gate 2: per-PR ratio.
    total_deletions = 0
    total_additions = 0
    for f in pr_files:
        if not isinstance(f, dict):
            continue
        try:
            total_additions += int(f.get("additions") or 0)
        except (TypeError, ValueError):
            pass
        try:
            total_deletions += int(f.get("deletions") or 0)
        except (TypeError, ValueError):
            pass

    if (
        total_deletions > PER_PR_DELETIONS_TO_ADDITIONS_RATIO * total_additions
        and total_deletions > PER_PR_DELETIONS_FLOOR
        and not has_refactor_label
    ):
        citation = (
            f"PR-level: -{total_deletions}/+{total_additions} "
            f"(ratio {total_deletions / max(total_additions, 1):.2f}x; "
            f"trip @ >2x AND >{PER_PR_DELETIONS_FLOOR})"
        )
        return GuardrailResult(
            tripped=True,
            layer="per_pr",
            reason=(
                f"per-PR ratio gate tripped: "
                f"{total_deletions} deletions vs {total_additions} additions "
                f"({total_deletions / max(total_additions, 1):.2f}x; "
                f"floor=100); ticket carries no 'refactor' label"
            ),
            citations=[citation],
        )

    return GuardrailResult(tripped=False)


__all__ = [
    "GuardrailResult",
    "compute_destructive_guardrails",
    "PER_FILE_DELETION_RATIO",
    "PER_FILE_DELETION_ABSOLUTE_CAP",
    "PER_PR_DELETIONS_TO_ADDITIONS_RATIO",
    "PER_PR_DELETIONS_FLOOR",
    "DELETION_LICENSE_KEYWORDS",
    "REFACTOR_LABEL",
]
