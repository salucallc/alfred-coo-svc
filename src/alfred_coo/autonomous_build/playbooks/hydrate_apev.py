"""Playbook: hydrate Linear ticket bodies with the canonical APE/V heading.

Scans Backlog/In-Progress tickets in the active MC v1 GA projects. For
any ticket whose body has an inline ``APE/V:`` marker but lacks the
canonical ``## APE/V Acceptance (machine-checkable)`` heading, append a
new section that wraps the inline prose under the canonical heading.

Why this matters: builders dispatched on tickets without the canonical
heading try to fetch the plan-doc URL referenced in prose; when that URL
is minipc-local (``Z:/_planning/...``) the fetch 404s and the builder
escalates as a grounding gap. PR #340 forbids the fetch when the
canonical section is present, so hydrating the body removes the failure
mode entirely.

This playbook folds the one-off ``Z:/_tmp/file_hydrate_apev_headings.py``
script into autonomous action — when a fresh project is added or a new
ticket without the heading is filed, the doctor self-heals it within
``interval_seconds * (candidates / max_actions_per_tick)`` minutes.

Idempotent: skips tickets that already carry the canonical heading.
Bounded: at most ``max_actions_per_tick`` mutations per tick.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from .base import Playbook, PlaybookResult


logger = logging.getLogger("alfred_coo.autonomous_build.playbooks.hydrate_apev")


CANONICAL_HEADING = "## APE/V Acceptance (machine-checkable)"

# Active MC v1 GA project IDs. Add/remove as projects open and close.
# Mirrors ``Z:/_tmp/file_hydrate_apev_headings.py::PROJECTS`` so a
# manual run vs the playbook agree on which projects to scan.
DEFAULT_ACTIVE_PROJECTS: dict[str, str] = {
    "Cockpit-UX":   "5a014234-df36-47a0-9abb-eac093e27539",
    "MSSP-Ext":     "39e340a8-26d2-4439-8582-caf94a263c7e",
    "MSSP-Fed":     "a9d93b23-96b4-4a77-be18-b709f72fa3ce",
    "Agent-Ingest": "9db00c4f-17a4-4b7a-8cd8-ea62f45d55b8",
}


def _extract_apev_text(body: str) -> str | None:
    """Pull the prose immediately following an ``APE/V:`` marker.

    Stops at: end of next paragraph (double newline), end of body, or
    any subsequent section heading (``^#``). Returns ``None`` if no
    marker is found or the marker has no following text.
    """
    for marker in ("APE/V:", "APEV:", r"Acceptance \(APE/V\):"):
        m = re.search(rf"{marker}\s*", body)
        if not m:
            continue
        rest = body[m.end():]
        cut = re.search(r"\n\s*\n|\n#", rest)
        text = rest[:cut.start()] if cut else rest[:2000]
        text = text.strip()
        if text:
            return text
    return None


def _render_canonical_section(apev_text: str) -> str:
    """Wrap apev_text under the canonical heading. Byte-stable so re-runs
    that somehow squeezed past the idempotency check would still produce
    the same body, not a divergent one."""
    return f"\n\n{CANONICAL_HEADING}\n\n{apev_text}\n"


class HydrateAPEVHeadingsPlaybook(Playbook):
    """See module docstring."""

    kind = "hydrate_apev_headings"
    max_actions_per_tick = 5

    def __init__(self, projects: dict[str, str] | None = None):
        self.projects = (
            projects if projects is not None else DEFAULT_ACTIVE_PROJECTS
        )

    async def execute(
        self,
        *,
        linear_api_key: str,
        dry_run: bool,
    ) -> PlaybookResult:
        result = PlaybookResult(kind=self.kind, dry_run=dry_run)
        if not linear_api_key:
            result.errors.append("linear_api_key missing")
            return result

        candidates: list[dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                for name, pid in self.projects.items():
                    try:
                        cands = await self._scan_project(
                            client, linear_api_key, name, pid,
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.exception("hydrate_apev: scan failed for %s", name)
                        result.errors.append(
                            f"{name}: scan_failed: {type(e).__name__}"
                        )
                        continue
                    candidates.extend(cands)
        except Exception as e:  # noqa: BLE001 — client setup/teardown
            logger.exception("hydrate_apev: client lifecycle failed")
            result.errors.append(
                f"client_lifecycle_failed: {type(e).__name__}"
            )
            return result

        result.candidates_found = len(candidates)
        if not candidates:
            return result

        to_act = candidates[: self.max_actions_per_tick]
        result.actions_skipped = max(0, len(candidates) - len(to_act))

        if dry_run:
            for c in to_act:
                result.notable.append(f"would hydrate {c['identifier']}")
            return result

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                for c in to_act:
                    try:
                        await self._patch_ticket(client, linear_api_key, c)
                        result.actions_taken += 1
                        result.notable.append(f"hydrated {c['identifier']}")
                    except Exception as e:  # noqa: BLE001
                        logger.exception(
                            "hydrate_apev: patch failed for %s", c["identifier"],
                        )
                        result.errors.append(
                            f"{c['identifier']}: {type(e).__name__}"
                        )
        except Exception as e:  # noqa: BLE001
            logger.exception("hydrate_apev: mutation client lifecycle failed")
            result.errors.append(
                f"mutation_client_lifecycle_failed: {type(e).__name__}"
            )
        return result

    async def _scan_project(
        self,
        client: httpx.AsyncClient,
        key: str,
        name: str,
        project_id: str,
    ) -> list[dict[str, Any]]:
        q = """query Q($pid: String!) {
            project(id: $pid) {
                issues(first: 100) {
                    nodes { id identifier title description state { name } }
                }
            }
        }"""
        resp = await client.post(
            "https://api.linear.app/graphql",
            headers={"Authorization": key, "Content-Type": "application/json"},
            content=json.dumps(
                {"query": q, "variables": {"pid": project_id}}
            ).encode(),
        )
        data = resp.json()
        nodes = (
            (data.get("data") or {})
            .get("project", {})
            .get("issues", {})
            .get("nodes", [])
            or []
        )
        cands: list[dict[str, Any]] = []
        for n in nodes:
            desc = n.get("description") or ""
            state_name = (n.get("state") or {}).get("name", "")
            # Skip terminal states; rewriting a Done/Cancelled body is
            # noise and risks resurfacing stale tickets in dashboards.
            if state_name in ("Done", "Cancelled"):
                continue
            if CANONICAL_HEADING in desc:
                continue
            apev = _extract_apev_text(desc)
            if not apev:
                continue
            cands.append({
                "id": n["id"],
                "identifier": n["identifier"],
                "title": (n.get("title") or "")[:80],
                "state": state_name,
                "apev_text": apev,
                "current_body": desc,
                "project": name,
            })
        return cands

    async def _patch_ticket(
        self,
        client: httpx.AsyncClient,
        key: str,
        candidate: dict[str, Any],
    ) -> None:
        new_body = (
            candidate["current_body"].rstrip()
            + _render_canonical_section(candidate["apev_text"])
        )
        mut = """mutation U($id: String!, $body: String!) {
            issueUpdate(id: $id, input: { description: $body }) { success }
        }"""
        resp = await client.post(
            "https://api.linear.app/graphql",
            headers={"Authorization": key, "Content-Type": "application/json"},
            content=json.dumps(
                {"query": mut, "variables": {"id": candidate["id"], "body": new_body}}
            ).encode(),
        )
        data = resp.json()
        ok = (
            (data.get("data") or {}).get("issueUpdate", {}).get("success")
        )
        if not ok:
            raise RuntimeError(f"issueUpdate not ok: {str(data)[:200]}")
