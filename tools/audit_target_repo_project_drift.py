"""SAL-4120 — cross-project drift audit.

Walks every Saluca Linear project listed in :data:`PROJECT_REPO_SCOPE`,
parses each issue's ``## Target`` block, and reports issues whose target
``owner/repo`` is NOT in the project's expected scope. The orchestrator's
runtime guard (``orchestrator._filter_out_of_scope_tickets``) catches
these defensively at wave-cohort assembly; this script catches them
proactively across the full backlog so the operator can move them to the
correct project before the next wave fires.

Usage::

    uv run python tools/audit_target_repo_project_drift.py
    # exit 0 = no drift; exit 1 = drift detected (non-empty report)

Output is a Markdown table to stdout; pipe to a file for archival or to
``cat`` it inline. The body parser is reused from the orchestrator's
graph module so this script and the runtime guard agree on what counts as
a Target block.

Env::

    LINEAR_API_KEY    Linear personal API key (raw, not Bearer-prefixed).
                      Falls back to ALFRED_OPS_LINEAR_API_KEY for parity
                      with the daemon's env conventions.

Notes
-----
* The PROJECT_REPO_SCOPE map below MUST stay in sync with
  ``orchestrator.ORCHESTRATOR_REPO_SCOPE``. To keep that link cheap, this
  script imports the runtime constant directly when ``alfred_coo`` is on
  ``sys.path`` and falls back to a hardcoded copy otherwise (so the
  script is runnable from a clean checkout without an editable install).
* Tickets whose body has no Target block, or whose Target block lacks
  owner/repo, are NOT flagged — they're handled by the legacy
  ``_TARGET_HINTS`` registry path inside the orchestrator. Only concrete
  ``owner/repo`` targets that fall outside the project's scope show up in
  the report.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

# ── Project → expected repo-scope map ──────────────────────────────────────
#
# Mirrors `alfred_coo.autonomous_build.orchestrator.ORCHESTRATOR_REPO_SCOPE`.
# Try to import the live constant (so this script + the runtime guard can
# never drift). Fall back to a hardcoded copy when the import fails (e.g.
# the script is run from a clean checkout without `uv sync`).
try:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from alfred_coo.autonomous_build.orchestrator import (  # type: ignore
        ORCHESTRATOR_REPO_SCOPE as _LIVE_SCOPE,
    )
    PROJECT_REPO_SCOPE: Dict[str, FrozenSet[str]] = dict(_LIVE_SCOPE)
except Exception:  # pragma: no cover — defensive fallback only
    PROJECT_REPO_SCOPE = {
        "8c1d8f69-359d-457a-a11c-2e650863774c": frozenset({
            "salucallc/alfred-coo-svc",
        }),
        "5a014234-df36-47a0-9abb-eac093e27539": frozenset({
            "salucallc/alfred-coo-svc",
        }),
        "39e340a8-26d2-4439-8582-caf94a263c7e": frozenset({
            "salucallc/alfred-coo-svc",
        }),
        "a9d93b23-96b4-4a77-be18-b709f72fa3ce": frozenset({
            "salucallc/alfred-coo-svc",
        }),
        "9db00c4f-17a4-4b7a-8cd8-ea62f45d55b8": frozenset({
            "salucallc/alfred-coo-svc",
        }),
    }

# Likewise reuse the body parser so script + runtime stay in sync.
try:
    from alfred_coo.autonomous_build.graph import (  # type: ignore
        _parse_target_from_ticket_body,
    )
except Exception:  # pragma: no cover
    import re

    _LIST_ITEM_RE = re.compile(r"^[\-\*]\s+(.+?)\s*$")

    def _parse_target_from_ticket_body(body: Optional[str]) -> Optional[Dict[str, Any]]:
        """Minimal Target-block parser used only when the live import fails."""
        if not body:
            return None
        m = re.search(r"(?im)^##\s*Target\s*$", body)
        if not m:
            return None
        chunk = body[m.end():]
        end = re.search(r"(?im)^##\s+", chunk)
        if end:
            chunk = chunk[: end.start()]
        out: Dict[str, Any] = {}
        for line in chunk.splitlines():
            kv = re.match(r"^\s*([a-zA-Z_]+)\s*:\s*(.*?)\s*$", line)
            if kv:
                out[kv.group(1).lower()] = kv.group(2).strip()
        return out or None

LINEAR_API_URL = "https://api.linear.app/graphql"


def _resolve_api_key() -> str:
    key = os.environ.get("LINEAR_API_KEY") or os.environ.get(
        "ALFRED_OPS_LINEAR_API_KEY"
    )
    if not key:
        raise SystemExit(
            "LINEAR_API_KEY (or ALFRED_OPS_LINEAR_API_KEY) must be set"
        )
    return key


def _gql(api_key: str, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        LINEAR_API_URL,
        data=payload,
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310 — fixed URL
        return json.loads(resp.read().decode("utf-8"))


_PROJECT_ISSUES_QUERY = """
query ProjectIssues($id: String!, $after: String) {
  project(id: $id) {
    id
    name
    issues(first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { id identifier description state { name } }
    }
  }
}
"""


def _fetch_project_issues(api_key: str, project_id: str) -> Tuple[str, List[Dict[str, Any]]]:
    issues: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    project_name = project_id
    while True:
        data = _gql(api_key, _PROJECT_ISSUES_QUERY, {"id": project_id, "after": cursor})
        proj = (data.get("data") or {}).get("project") or {}
        if not proj:
            break
        project_name = proj.get("name") or project_name
        page = proj.get("issues") or {}
        issues.extend(page.get("nodes") or [])
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        cursor = info.get("endCursor")
    return project_name, issues


def _audit() -> List[Dict[str, str]]:
    """Return list of drift records (one per out-of-scope ticket)."""
    api_key = _resolve_api_key()
    drift: List[Dict[str, str]] = []
    for project_id, expected_scope in sorted(PROJECT_REPO_SCOPE.items()):
        try:
            project_name, issues = _fetch_project_issues(api_key, project_id)
        except Exception as exc:  # noqa: BLE001 — best-effort per project
            print(
                f"WARN: failed to fetch project {project_id}: {exc}",
                file=sys.stderr,
            )
            continue
        scope_str = ",".join(sorted(expected_scope))
        for issue in issues:
            body = issue.get("description") or ""
            try:
                parsed = _parse_target_from_ticket_body(body)
            except Exception:
                parsed = None
            if not parsed:
                continue
            owner = (parsed.get("owner") or "").strip()
            repo = (parsed.get("repo") or "").strip()
            if not owner or not repo:
                continue
            target_repo = f"{owner}/{repo}"
            if target_repo in expected_scope:
                continue
            drift.append({
                "ticket": issue.get("identifier") or issue.get("id") or "?",
                "project": project_name,
                "target_repo": target_repo,
                "expected_scope": scope_str,
                "state": (issue.get("state") or {}).get("name") or "?",
            })
    return drift


def _render_markdown(drift: List[Dict[str, str]]) -> str:
    lines = [
        "# Cross-project Target-repo drift report (SAL-4120)",
        "",
        f"Total drift records: **{len(drift)}**",
        "",
    ]
    if not drift:
        lines.append("No drift detected. All tickets target a repo within their project's scope.")
        return "\n".join(lines) + "\n"
    lines += [
        "| Ticket | Project | Target repo | Expected scope | State |",
        "| --- | --- | --- | --- | --- |",
    ]
    for d in sorted(drift, key=lambda r: (r["project"], r["ticket"])):
        lines.append(
            f"| {d['ticket']} | {d['project']} | `{d['target_repo']}` | "
            f"`{d['expected_scope']}` | {d['state']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    drift = _audit()
    sys.stdout.write(_render_markdown(drift))
    return 0 if not drift else 1


if __name__ == "__main__":
    raise SystemExit(main())
