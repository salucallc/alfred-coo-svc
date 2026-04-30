"""Tool registry for the Alfred COO daemon.

Phase B.3.1: OpenAI-compatible tool-use. Each ToolSpec carries its JSON schema
(for the model), a short human-readable description, and an async handler that
implements the actual effect. The dispatch loop renders all enabled tools to
OpenAI function schema, calls the model in a multi-turn loop, executes any
tool_calls the model emits, and returns the final answer once the model stops
requesting tools.

Enabling tool-use is OPT-IN per persona via `persona.tools` (a list of tool
names). Personas with an empty list keep the B.2 structured-output path. This
keeps backward compatibility while the tool set stabilises.

Tool handlers return JSON-serialisable dicts — these are fed back to the model
as `role=tool` content. Handlers that raise are caught and the error string
goes back to the model as the tool result, so one bad invocation never aborts
the dispatch loop.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import ast
import json
import logging
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional

# SAL-2905: per-persona GitHub identity routing. ``token_for_persona``
# replaces direct ``os.environ.get("GITHUB_TOKEN")`` reads inside the
# GitHub-touching tool handlers; ``GitHubIdentityClass`` exposes the
# class tags for fallback decisions.
from .persona_github import (
    GitHubIdentityClass,
    get_current_persona,
    token_for_persona,
)


# Current-task context for tool handlers. main.py sets this around
# call_with_tools so handlers that need task scoping (e.g. propose_pr
# workspaces) pick up the real task id without the model having to pass it.
_current_task_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "alfred_coo_current_task_id", default=None
)


def get_current_task_id() -> Optional[str]:
    return _current_task_id.get()


def set_current_task_id(task_id: Optional[str]):
    """Returns a token for resetting via _current_task_id.reset(token)."""
    return _current_task_id.set(task_id)


def reset_current_task_id(token) -> None:
    _current_task_id.reset(token)


def _github_token_for(
    intended_class: str,
    *,
    persona_override: Optional[str] = None,
) -> tuple[str, str]:
    """Internal helper: resolve a token for a tool's intended identity class.

    Each tool knows the class it *should* run as (builder writes PRs,
    QA submits reviews, orchestrator merges). The tool's intended
    class is authoritative for token routing — that's the whole point
    of split-identity: pr_review must always use the QA token, even
    when (e.g.) a builder-persona ContextVar happens to be active.

    Resolution rules:
      1. Try ``GITHUB_TOKEN_<CLASS>`` for the intended class. If set,
         return it tagged as that class.
      2. ORCHESTRATOR-class only: fall back to ``GITHUB_TOKEN_QA`` if
         set (semantic: "QA approves → QA merges" when no dedicated
         orchestrator bot exists).
      3. Fall through to legacy ``GITHUB_TOKEN`` (single-token mode).
      4. Nothing configured → return ``("", "unknown")`` so the
         caller's existing missing-token error fires.

    The ``persona_override`` argument is reserved for future use by
    callers that want to explicitly opt out of intended-class routing
    (e.g. a builder-driven http_get on a github.com URL where the
    persona's identity is the audit-relevant one). When non-None and
    the persona resolves to a different class than ``intended_class``,
    the persona wins. Default behaviour (intended-class authoritative)
    matches the design doc §4.4.
    """
    # The current persona context is captured for diagnostics only —
    # the intended class drives token resolution.
    active_persona = (
        persona_override
        if persona_override is not None
        else get_current_persona()
    )
    _ = active_persona  # diagnostic only; future log-line hook

    # Direct class → env-var lookup. Mirrors persona_github._TOKEN_ENV_VARS
    # without having to import the private dict.
    class_env_vars = {
        GitHubIdentityClass.BUILDER: "GITHUB_TOKEN_BUILDER",
        GitHubIdentityClass.QA: "GITHUB_TOKEN_QA",
        GitHubIdentityClass.ORCHESTRATOR: "GITHUB_TOKEN_ORCHESTRATOR",
    }
    env_var = class_env_vars.get(intended_class)
    if env_var:
        token = os.environ.get(env_var, "").strip()
        if token:
            return token, intended_class

    # ORCHESTRATOR fallback to QA — see design doc §4.4.
    if intended_class == GitHubIdentityClass.ORCHESTRATOR:
        qa_token = os.environ.get(
            class_env_vars[GitHubIdentityClass.QA], ""
        ).strip()
        if qa_token:
            return qa_token, GitHubIdentityClass.QA

    # Legacy single-token catch-all.
    legacy = os.environ.get("GITHUB_TOKEN", "").strip()
    if legacy:
        return legacy, GitHubIdentityClass.UNKNOWN

    return "", GitHubIdentityClass.UNKNOWN


logger = logging.getLogger("alfred_coo.tools")

ToolHandler = Callable[..., Awaitable[Dict[str, Any]]]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: ToolHandler


def openai_tool_schema(spec: ToolSpec) -> Dict[str, Any]:
    """Render a ToolSpec as an OpenAI-compatible tool declaration."""
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
    }


# ── Built-in tool handlers ──────────────────────────────────────────────────

SAL_TEAM_ID = "03ee70b4-ed03-4305-a3ae-4556afb06b04"
LINEAR_GRAPHQL = "https://api.linear.app/graphql"


async def linear_create_issue(
    title: str,
    description: str = "",
    priority: int = 3,
    due_date: Optional[str] = None,
    labels: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Create a Linear issue in the SAL team. Returns {identifier, url}."""
    key = os.environ.get("LINEAR_API_KEY") or os.environ.get("ALFRED_OPS_LINEAR_API_KEY")
    if not key:
        return {"error": "LINEAR_API_KEY not configured"}

    mutation = (
        "mutation IssueCreate($input: IssueCreateInput!) { "
        "issueCreate(input: $input) { success issue { identifier url title dueDate } } }"
    )
    variables: Dict[str, Any] = {
        "input": {
            "teamId": SAL_TEAM_ID,
            "title": title,
            "description": description or "",
            "priority": priority,
        }
    }
    if due_date:
        variables["input"]["dueDate"] = due_date

    payload = json.dumps({"query": mutation, "variables": variables}).encode()
    req = urllib.request.Request(
        LINEAR_GRAPHQL,
        data=payload,
        headers={
            "Authorization": key,
            "Content-Type": "application/json",
            "User-Agent": "saluca-alfred/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": f"linear http {e.code}: {e.read().decode()[:300]}"}
    except Exception as e:
        return {"error": f"linear transport: {type(e).__name__}: {e}"}

    iss = (body.get("data") or {}).get("issueCreate") or {}
    if not iss.get("success"):
        return {"error": "linear returned success=false", "raw": body}
    out = iss.get("issue") or {}
    return {
        "identifier": out.get("identifier"),
        "url": out.get("url"),
        "title": out.get("title"),
        "due_date": out.get("dueDate"),
    }


async def slack_post(
    message: str,
    channel: Optional[str] = None,
) -> Dict[str, Any]:
    """Post a message to Slack. Defaults to the batcave channel."""
    token = os.environ.get("SLACK_BOT_TOKEN") or os.environ.get("SLACK_BOT_TOKEN_ALFRED")
    if not token:
        return {"error": "SLACK_BOT_TOKEN not configured"}
    target = channel or os.environ.get("SLACK_BATCAVE_CHANNEL") or "C0ASAKFTR1C"

    payload = json.dumps({"channel": target, "text": message}).encode("utf-8")
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read())
    except Exception as e:
        return {"error": f"slack transport: {type(e).__name__}: {e}"}

    if not body.get("ok"):
        return {"error": f"slack {body.get('error', 'unknown')}", "raw": body}
    return {"ts": body.get("ts"), "channel": body.get("channel")}


# ── mesh_task_create ────────────────────────────────────────────────────────

SOUL_API_URL_DEFAULT = "http://100.105.27.63:8080"


async def mesh_task_create(
    title: str,
    description: str = "",
    persona: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Create a mesh task that other daemon personas can claim.

    If `persona` is given we prepend [persona:<name>] to the title so the daemon
    parser routes it to that persona on claim. Extra free-form tags become
    bracketed prefixes as well.
    """
    soul_url = (os.environ.get("SOUL_API_URL") or SOUL_API_URL_DEFAULT).rstrip("/")
    soul_key = os.environ.get("SOUL_API_KEY")
    session_id = os.environ.get("SOUL_SESSION_ID") or "alfred-coo"
    if not soul_key:
        return {"error": "SOUL_API_KEY not configured"}

    prefixes = []
    if persona:
        prefixes.append(f"[persona:{persona}]")
    for t in tags or []:
        if t and not t.startswith("["):
            prefixes.append(f"[{t}]")
    full_title = (" ".join(prefixes) + (" " if prefixes else "") + title).strip()

    payload = json.dumps({
        "from_session_id": session_id,
        "title": full_title,
        "description": description or "",
    }).encode()
    req = urllib.request.Request(
        f"{soul_url}/v1/mesh/tasks",
        data=payload,
        headers={
            "Authorization": f"Bearer {soul_key}",
            "Content-Type": "application/json",
            "User-Agent": "saluca-alfred/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": f"soul-svc http {e.code}: {e.read().decode()[:300]}"}
    except Exception as e:
        return {"error": f"mesh transport: {type(e).__name__}: {e}"}

    return {
        "id": body.get("id"),
        "title": body.get("title"),
        "status": body.get("status"),
    }


# ── propose_pr ──────────────────────────────────────────────────────────────

WORKSPACE_ROOT = Path(os.environ.get("ALFRED_WORKSPACES_ROOT") or "/var/lib/alfred-coo/workspaces")
_VALID_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,38}$")
_VALID_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# ── SAL-2953: APE/V citation auto-inject ────────────────────────────────────
#
# Hawkman's GATE 1 (see persona.py "GATE 1 — APE/V citation requirement")
# REQUEST_CHANGES every PR whose body lacks a verbatim APE/V citation.
# In v7y wave-1 this cost 3 review cycles across 2 dispatched tickets
# (SAL-2584 finally APPROVED on cycle #3; SAL-2610 escalated). The
# orchestrator-side fix is deterministic: at propose_pr time,
# if the builder's body is missing the `## APE/V` (or `## APE/V Citation`)
# heading, synthesise one from the canonical Linear ticket body
# (with a plan-doc fallback when Linear is unreachable) and append it.
#
# The block format mirrors the canonical APPROVED PR bodies on
# salucallc/alfred-coo-svc#96 and salucallc/tiresias-sovereign#8:
#
#     ## APE/V Citation
#     - Plan doc path: `plans/v1-ga/<TICKET>.md`
#     - Verification: <one-line summary>
#     - Acceptance criteria:
#     ```
#     <verbatim acceptance lines>
#     ```
#
# Idempotent: if the body already carries any `## APE/V` heading
# (case-insensitive, with or without the literal slash) the helper
# returns the body unchanged. The builder LLM is therefore free to
# emit its own citation; we only fill in when it forgets.
#
# ── SAL-2965: source = Linear ticket body, skip on fix-round ────────────────
#
# The original SAL-2953 implementation extracted acceptance criteria from
# the per-ticket plan doc (`plans/v1-ga/<CODE>.md`). Three failure modes
# observed in v7z (PR #103 SAL-2601 ALT-04 confirmed gate-1 is byte-
# verbatim substring against Linear, not format-only):
#
#   1. Source drift. The plan doc is builder-authored; on a hawkman fix-
#      round respawn the builder rewrites it and sometimes fills the
#      acceptance section with the fix-round directive ("Address every
#      point in the review feedback below…") instead of the upstream
#      APE/V text. Hawkman validates against the *Linear ticket body's*
#      acceptance section, which is canonical, so the auto-injected
#      citation no longer matched and GATE 1 stayed red. Concrete case:
#      salucallc/soul-svc#37 (SAL-2613) — auto-inject shipped the fix-
#      round directive verbatim as "acceptance criteria".
#   2. Fix-round overwrite. `update_pr` re-ran the auto-inject on every
#      respawn. If the original PR body had the citation built from the
#      previous (clean) plan doc, a follow-up rewrite with a drifted
#      plan doc would replace it with stale text — or worse, append a
#      second drifted block if the canonical heading regex missed.
#   3. Paraphrase drift. PR #103 (SAL-2601) shipped a fenced citation
#      block whose contents had been *paraphrased* — semicolons rewritten
#      to periods, tuples re-quoted with backticks, the trailing "and
#      green" dropped. Hawkman REQUEST_CHANGES because it does a byte-
#      verbatim substring match against the Linear ticket body, not a
#      semantic / format check. The helper therefore must NOT perform
#      any string normalisation, markdown enrichment, or stylistic
#      rewriting on the extracted text — what comes out of Linear must
#      land inside the fenced block byte-for-byte.
#
# The SAL-2965 fix tightens all three:
#
#   • Source: prefer Linear ticket body. The orchestrator already has
#     `LINEAR_API_KEY` configured for `linear_create_issue` and
#     `linear_update_issue_state`; we reuse it to GET the issue body
#     by `identifier` (the ticket code) and parse the acceptance
#     section from there.
#   • Heading variants: Mission Control v1 GA tickets use
#     `## APE/V Acceptance (machine-checkable)`; older tickets and plan
#     docs use `## Acceptance criteria`. The extraction regex accepts
#     both (and minor stylistic variants).
#   • Verbatim: the extraction strips outer whitespace only — content
#     between the heading and the next heading is preserved byte-for-
#     byte. No normalisation, no rewriting, no enrichment.
#   • Fallback: when Linear is unreachable (no key, transport error,
#     section missing) the helper falls back to the plan-doc path so
#     air-gapped / fixture-driven tests stay green.
#   • Skip on fix-round: `update_pr` calls pass `is_fix_round=True` so
#     the helper short-circuits without touching the body. The original
#     PR body's citation is preserved across fix-rounds; the builder is
#     free to re-edit the body explicitly if they choose.
_APEV_HEADING_RE = re.compile(
    r"(?im)^\s{0,3}#{2,3}\s*APE\s*[/\-_]?\s*V\b"
)
_TICKET_CODE_RE = re.compile(r"\b(SAL-\d+)\b", re.IGNORECASE)
_PLAN_DOC_PATH_RE = re.compile(
    r"^plans/v1-ga/(?P<code>[A-Za-z0-9][A-Za-z0-9_-]+)\.md$"
)
_PLAN_ACCEPTANCE_RE = re.compile(
    # SAL-2965 (post-evidence-2026-04-26): hawkman validates byte-verbatim
    # substring against the *Linear ticket body's* acceptance section.
    # Mission Control v1 GA tickets use `## APE/V Acceptance (machine-
    # checkable)` (verified on SAL-2601 / SAL-2613 / SAL-2611). Older
    # plan docs and historical tickets use `## Acceptance criteria`.
    # The regex accepts both — and any common stylistic variant — so the
    # helper extracts the same canonical text whether the source is the
    # Linear `description` field or a `plans/v1-ga/<CODE>.md` doc.
    #
    # Permissive on the heading wording, strict on what counts as the
    # *body*: capture stops at the next markdown heading (h1-h3) or EOF
    # so we do not bleed into `## Effort` / `## Notes` / etc. The capture
    # is deliberately raw — no whitespace collapsing, no markdown
    # rewriting, no semicolon-to-period substitution. Whatever bytes the
    # ticket author wrote between the heading and the next section MUST
    # appear byte-identical inside the auto-injected fenced block.
    r"(?ims)"
    r"^\s{0,3}#{2,3}\s*"  # heading marker (## or ###)
    r"(?:APE\s*[/\-_]?\s*V\s+)?"  # optional "APE/V" / "APE-V" / "APEV" prefix
    r"Acceptance"  # core word
    r"(?:\s+criteria)?"  # optional "criteria" suffix (plan-doc style)
    r"(?:\s*\([^)]*\))?"  # optional parenthetical e.g. "(machine-checkable)"
    r"\s*\n"
    r"(?P<body>.*?)"
    r"(?=^\s{0,3}#{1,3}\s|\Z)"
)


def _apev_body_has_citation(body: Optional[str]) -> bool:
    """True iff the PR body already carries an APE/V citation heading.

    Matches `## APE/V`, `## APE-V`, `## APEV`, `### APE/V Citation`, etc.
    The heading regex is intentionally permissive so the auto-inject does
    not fight a builder that picked a slight stylistic variant — the
    hawkman LLM persona accepts any heading that visibly groups the
    acceptance citation.
    """
    if not body or not isinstance(body, str):
        return False
    return bool(_APEV_HEADING_RE.search(body))


def _extract_ticket_code(*sources: Optional[str]) -> Optional[str]:
    """Pull a SAL-NNNN ticket code out of the first source that contains one.

    Used to fall back to the branch name (`feature/sal-2953-x`) or PR
    title when the files dict has no plan doc to disambiguate the
    citation.
    """
    for src in sources:
        if not src:
            continue
        m = _TICKET_CODE_RE.search(str(src))
        if m:
            return m.group(1).upper()
    return None


def _find_plan_doc_in_files(
    files: Mapping[str, str],
    *,
    ticket_code: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Locate the per-ticket plan doc in the propose_pr / update_pr files.

    Returns ``(path, content)`` for the first ``plans/v1-ga/<CODE>.md``
    entry. When ``ticket_code`` is given, prefer an exact filename match;
    otherwise return the first plan-doc-shaped path.
    """
    if not files:
        return (None, None)
    fallback: tuple[Optional[str], Optional[str]] = (None, None)
    for path, content in files.items():
        if not isinstance(path, str) or not isinstance(content, str):
            continue
        m = _PLAN_DOC_PATH_RE.match(path.strip())
        if not m:
            continue
        code = m.group("code").upper()
        if ticket_code and code == ticket_code.upper():
            return (path, content)
        if fallback[0] is None:
            fallback = (path, content)
    return fallback


def _extract_acceptance_lines(plan_doc_content: Optional[str]) -> Optional[str]:
    """Pull the acceptance section body out of a plan-doc / Linear-ticket markdown.

    Recognised section headings (case-insensitive, h2 or h3):
      * ``## APE/V Acceptance (machine-checkable)`` — Mission Control v1
        GA ticket convention (canonical, what hawkman validates against).
      * ``## APE/V Acceptance`` — same, without the parenthetical.
      * ``## Acceptance criteria`` — historical plan-doc convention.
      * ``## Acceptance`` — terse variant.

    Returns the section's text with leading/trailing whitespace trimmed
    only — content between the heading and the next heading is preserved
    BYTE-FOR-BYTE. No newline collapsing, no semicolon-to-period
    rewriting, no backtick wrapping, no markdown enrichment. Hawkman's
    GATE 1 is a verbatim substring match against the Linear ticket body;
    any post-processing here introduces drift and breaks the gate.

    Returns ``None`` when the section is missing or the body is empty
    after outer-whitespace trim. Callers fall back to the plan-doc path
    on ``None`` from the Linear-bound fetcher.
    """
    if not plan_doc_content or not isinstance(plan_doc_content, str):
        return None
    m = _PLAN_ACCEPTANCE_RE.search(plan_doc_content)
    if not m:
        return None
    return (m.group("body") or "").strip() or None


def _build_apev_citation_block(
    *,
    plan_doc_path: Optional[str],
    acceptance_lines: Optional[str],
    verification: Optional[str] = None,
    ticket_code: Optional[str] = None,
) -> str:
    """Assemble an APE/V citation block in hawkman's expected shape.

    Mirrors the body of ac#96 / tir#8 (the two APPROVED-on-citation
    PRs). The ``Verification`` bullet is a stub when the orchestrator
    can't infer one — hawkman's GATE 1 grep matches on the heading +
    plan-doc path + acceptance-criteria fenced block, not on the
    verification wording.
    """
    plan_path = plan_doc_path
    if not plan_path and ticket_code:
        plan_path = f"plans/v1-ga/{ticket_code.upper()}.md"
    plan_path_line = (
        f"- Plan doc path: `{plan_path}`"
        if plan_path
        else "- Plan doc path: (not provided)"
    )
    verif_line = (
        f"- Verification: {verification.strip()}"
        if verification and verification.strip()
        else "- Verification: see PR diff and CI run for the acceptance checks below."
    )
    if acceptance_lines and acceptance_lines.strip():
        criteria_block = f"```\n{acceptance_lines.strip()}\n```"
    else:
        criteria_block = (
            "```\n"
            "(Acceptance criteria not extractable from plan doc; see "
            f"{plan_path or 'plan doc'} for the verbatim APE/V section.)\n"
            "```"
        )
    return (
        "\n\n"
        "## APE/V Citation\n"
        f"{plan_path_line}\n"
        f"{verif_line}\n"
        "- Acceptance criteria:\n"
        f"{criteria_block}\n"
    )


def _fetch_linear_acceptance_criteria(
    ticket_code: Optional[str],
) -> Optional[str]:
    """Fetch the acceptance section from a Linear ticket body, byte-verbatim.

    SAL-2965: hawkman validates GATE 1 with a byte-verbatim substring
    match against the *Linear ticket body's* acceptance section, not the
    plan doc's. The plan doc is builder-authored and drifts (especially
    on fix-round respawns where the builder pastes the fix-round
    directive into the section). Pulling canonical text from Linear and
    embedding it byte-for-byte closes that drift surface.

    Mission Control v1 GA tickets use the heading
    ``## APE/V Acceptance (machine-checkable)``. Older tickets and plan
    docs use ``## Acceptance criteria``. ``_extract_acceptance_lines``
    accepts both forms.

    Returns the section body trimmed at the outer edges only — every
    byte between the heading line and the next markdown heading is
    preserved exactly as Linear stored it. ``None`` when:
      - ``ticket_code`` is empty / unparseable,
      - ``LINEAR_API_KEY`` (or ``ALFRED_OPS_LINEAR_API_KEY``) is unset,
      - the Linear GraphQL request fails (HTTP / transport),
      - the issue has no body or no recognised acceptance heading.

    Callers fall back to the plan-doc path on ``None``.

    Synchronous: ``_maybe_inject_apev_citation`` is called from inside
    sync code paths in ``propose_pr`` / ``update_pr`` flows, and the
    helper is best-effort (a Linear hiccup must not block PR creation).
    """
    if not ticket_code:
        return None
    key = os.environ.get("LINEAR_API_KEY") or os.environ.get(
        "ALFRED_OPS_LINEAR_API_KEY"
    )
    if not key:
        return None

    # Linear's GraphQL `issue(id: ID!)` accepts the human identifier
    # (e.g. "SAL-2965") — the field is named `id` but takes either UUID
    # or identifier. Verified against the SAL-2965 / SAL-2611 tickets.
    query = (
        "query IssueBody($id: String!) { "
        "issue(id: $id) { identifier description } }"
    )
    payload = json.dumps({
        "query": query,
        "variables": {"id": ticket_code},
    }).encode()
    req = urllib.request.Request(
        LINEAR_GRAPHQL,
        data=payload,
        headers={
            "Authorization": key,
            "Content-Type": "application/json",
            "User-Agent": "saluca-alfred/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read())
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return None
    except Exception:
        return None

    issue = (body.get("data") or {}).get("issue") or {}
    description = issue.get("description")
    if not description or not isinstance(description, str):
        return None
    # Reuse the same regex the plan-doc path uses — Linear ticket bodies
    # carry the same `## Acceptance criteria` heading convention.
    return _extract_acceptance_lines(description)


def _gate_a_apev_byte_match(
    body: Optional[str],
    *,
    branch: Optional[str] = None,
    title: Optional[str] = None,
    pr_url: Optional[str] = None,
    files: Optional[Mapping[str, str]] = None,
    linear_fetcher: Optional[Callable[[Optional[str]], Optional[str]]] = None,
) -> Optional[str]:
    """Gate A (autonomy): verify the Linear ticket's APE/V acceptance
    section appears byte-for-byte in the PR body.

    Hawkman GATE 1 does a verbatim substring match against the Linear
    ticket body's ``## APE/V Acceptance (machine-checkable)`` section.
    The auto-inject in ``_maybe_inject_apev_citation`` only fires when
    the *heading* is absent — paraphrased headings like
    ``## APE/V Citation`` or ``## Acceptance criteria`` skip the inject
    AND fail Hawkman GATE 1 → REQUEST_CHANGES → wasted cycle.

    This gate runs AFTER auto-inject and BEFORE the GitHub API call.
    Returns ``None`` to pass, or a string error to abort propose_pr.

    Fail-open conditions (gate is silent, returns None):
      - No ticket code parseable from branch/title/pr_url/body
      - Linear API key unset (fetcher returns None)
      - Linear has no acceptance section for this ticket
      - Network glitch fetching from Linear

    These all reduce to "we couldn't fetch canonical text", which is
    not the same as "builder shipped wrong text". Better to ship a PR
    and let Hawkman do the deeper check than to block on infra.
    """
    if not body:
        return (
            "GATE_A_APEV_EMPTY_BODY: PR body is empty; "
            "Hawkman GATE 1 will REQUEST_CHANGES."
        )
    ticket_code = _extract_ticket_code(branch, title, pr_url, body)
    if not ticket_code:
        return None  # fail-open: can't identify the ticket
    fetcher = linear_fetcher or _fetch_linear_acceptance_criteria
    canonical = fetcher(ticket_code)
    if not canonical:
        return None  # fail-open: no canonical source
    canonical = canonical.strip()
    if not canonical:
        return None
    if canonical in body:
        return None  # PASS: byte-substring present
    # Try a slightly looser comparison: collapse runs of whitespace.
    # Hawkman's actual regex tolerates trailing-whitespace normalisation
    # but not bullet rewrites or word reordering, so this is a small
    # safety margin — not a substitute for verbatim copy.
    canon_normal = re.sub(r"[ \t]+\n", "\n", canonical).strip()
    body_normal = re.sub(r"[ \t]+\n", "\n", body)
    if canon_normal and canon_normal in body_normal:
        return None
    preview = canonical[:400] + ("..." if len(canonical) > 400 else "")
    return (
        f"GATE_A_APEV_NOT_VERBATIM: the canonical Linear acceptance "
        f"section for {ticket_code} is not present byte-for-byte in the "
        f"PR body. Hawkman GATE 1 will REQUEST_CHANGES. Re-emit the PR "
        f"body with a `## APE/V Acceptance (machine-checkable)` section "
        f"containing exactly:\n\n{preview}"
    )


# Gate D regexes: extract route paths from FastAPI / Flask / Starlette decorators.
# Catches @router.get("/path"), @router.post("/path", ...), @app.put("/x"),
# @router.api_route("/y"), @app.delete("/z"), etc. Group 1 is the path.
_ROUTE_DECORATOR_RE = re.compile(
    r"""@(?:router|app)\.(?:get|post|put|patch|delete|head|options|api_route)\s*\(\s*['"]([^'"]+)['"]""",
    re.VERBOSE,
)
# Catches `app.include_router(some_router, prefix="/v1/foo")` and the
# `prefix=` form on `APIRouter(prefix="/v1/foo")`.
_INCLUDE_PREFIX_RE = re.compile(
    r"""(?:include_router|APIRouter)\s*\([^)]*?prefix\s*=\s*['"]([^'"]+)['"]""",
)
# Catches URL strings used in tests: client.get("/v1/foo"), httpx.post("/v1/bar"),
# requests.put("/v1/baz"). Conservative: only matches when the URL is a literal
# string starting with "/" — it's the test-file form Hawkman has been catching.
_TEST_URL_CALL_RE = re.compile(
    r"""(?:client|httpx|requests|conn|self\.client|TestClient\([^)]*\))\.(?:get|post|put|patch|delete|head|options)\s*\(\s*['"](/[^'"\s?#]+)""",
)


def _gate_d_endpoint_path_consistency(
    files: Optional[Mapping[str, str]],
) -> Optional[str]:
    """Gate D (autonomy): every URL path called from a test file in the
    propose_pr / update_pr files dict must be served by at least one
    route decorator (with optional include_router prefix) in a router
    file in the same files dict.

    Catches the soul-svc PR #66 cycle-3 pattern where the test called
    ``/v1/mssp/audit`` but the router mounted audit at
    ``/v1/mssp/consent/audit`` — Hawkman GATE 3 (target drift) rejected
    on every cycle.

    Fail-open conditions (gate is silent, returns None):
      - No files dict, or no test files in the dict
      - No router files in the dict (only adding tests; prior router
        change unrelated to this PR)
      - No URL calls extractable from tests

    These reduce to "this PR isn't adding both tests and routes
    together". Don't block — Hawkman still does the deeper check.

    Returns ``None`` to pass, or a string error to abort propose_pr.
    """
    if not files:
        return None
    test_urls: dict[str, str] = {}  # url → first test file that calls it
    decorator_paths: list[str] = []
    include_prefixes: list[str] = []
    for path, content in files.items():
        if not isinstance(path, str) or not isinstance(content, str):
            continue
        # Tests: extract URL calls
        if "/test" in path or path.startswith("test") or "/tests/" in path:
            for m in _TEST_URL_CALL_RE.finditer(content):
                url = m.group(1)
                test_urls.setdefault(url, path)
            continue
        # Routers / app modules: extract route decorators + include prefixes
        if (
            "router" in path.lower()
            or "/api" in path.lower()
            or path.endswith("main.py")
            or path.endswith("app.py")
            or path.endswith("__init__.py")
        ):
            for m in _ROUTE_DECORATOR_RE.finditer(content):
                decorator_paths.append(m.group(1))
            for m in _INCLUDE_PREFIX_RE.finditer(content):
                include_prefixes.append(m.group(1))
    if not test_urls or not decorator_paths:
        # Only one side of the contract is in this PR — can't check.
        return None
    # Build the full set of mountable paths: every decorator path,
    # plus every (prefix + decorator_path) combination. Conservative:
    # also accept exact-match decorator without prefix (some routers
    # expose paths at root).
    mounted: set[str] = set(decorator_paths)
    for pref in include_prefixes:
        for dec in decorator_paths:
            mounted.add(pref.rstrip("/") + dec)
    # Strip path parameters for fuzzy match: "/v1/foo/{id}" matches
    # test call "/v1/foo/abc-123".
    def _strip_params(p: str) -> str:
        return re.sub(r"\{[^}]+\}", "<param>", p)
    mounted_normalised = {_strip_params(p) for p in mounted}

    def _matches(test_url: str) -> bool:
        # Strip query string and fragment if any.
        u = test_url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        if u in mounted or u in mounted_normalised:
            return True
        # Try fuzzy: replace each path segment with <param> and see if
        # the test url shape matches a mounted shape.
        u_parts = u.split("/")
        for m in mounted_normalised:
            m_parts = m.split("/")
            if len(u_parts) != len(m_parts):
                continue
            if all(
                up == mp or mp == "<param>"
                for up, mp in zip(u_parts, m_parts)
            ):
                return True
        return False

    orphans = sorted(
        url for url in test_urls if not _matches(url)
    )
    if not orphans:
        return None
    sample = orphans[:5]
    sample_files = sorted({test_urls[u] for u in sample})
    return (
        f"GATE_D_ENDPOINT_DRIFT: {len(orphans)} test URL(s) call paths "
        f"that are not mounted by any router in this PR. Hawkman "
        f"GATE 3 (target drift) will REQUEST_CHANGES; the live HTTP "
        f"call would 404. Orphans: {sample!r}. Test files involved: "
        f"{sample_files!r}. Mounted paths in PR: "
        f"{sorted(mounted_normalised)[:10]!r}. "
        f"Either add the missing route(s) or fix the test URL(s)."
    )


# Gate B-lite: AST-based check for placeholder-only test functions.
# Earlier regex form (kept as `_BL_TRIVIAL_TEST_BODY_RE` for legacy unit
# tests) failed in production on 2026-04-30 because it could not match
# `def test_X` preceded by `@pytest.mark.X` decorators or bodies that
# interleave `# TODO` comment lines with the trivial assertion. AST
# parsing makes both invisible (decorators travel on the function node;
# comments are stripped before AST construction), so the gate becomes
# robust to any test-style boilerplate while remaining conservative —
# a single non-trivial statement makes the function not-a-placeholder.
#
# Trivial statements (after stripping a leading docstring):
#   - `pass`
#   - `assert <truthy literal>` (assert True / assert 1 / assert "x")
#   - `raise NotImplementedError(...)` or bare `NotImplementedError(...)`
# Anything else (call, comparison, mock setup, await, return) flips the
# function to non-trivial and the gate stays quiet.
_BL_TRIVIAL_TEST_BODY_RE = re.compile(
    r"""
    ^(?P<indent>[ \t]*)def\s+test_\w+\s*\([^)]*\)\s*(?:->\s*[^:]+)?\s*:\s*\n
    (?:[ \t]+(?:\"\"\".*?\"\"\"|'''.*?''')\s*\n)?
    (?P<body>(?:[ \t]+(?:pass|assert\s+True|raise\s+NotImplementedError(?:\([^)]*\))?|NotImplementedError(?:\([^)]*\))?)\s*(?:\#[^\n]*)?\n)+)
    """,
    re.VERBOSE | re.MULTILINE | re.DOTALL,
)
# `_BL_PLACEHOLDER_PLAN_DOC_RE` matches the literal phrase Hawkman's
# prompt rejects on: "placeholder implementations may need to be
# replaced" (case-insensitive). Plan docs that admit this are an
# automatic Hawkman REQUEST_CHANGES.
_BL_PLACEHOLDER_PLAN_DOC_RE = re.compile(
    r"placeholder\s+implementations?\s+(?:may\s+)?need(?:s)?\s+to\s+be\s+replaced",
    re.IGNORECASE,
)


def _is_placeholder_test_function(node: ast.AST) -> bool:
    """Return True iff ``node`` is a test function whose body, after
    stripping the leading docstring, contains only trivial statements
    (``pass``, ``assert <truthy literal>``, ``raise NotImplementedError``).

    Comments are absent from the AST, so ``# TODO`` lines and decorators
    do not affect this classification — that is the whole point of using
    AST instead of regex matching.
    """
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    if not node.name.startswith("test_"):
        return False
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]  # drop docstring
    if not body:
        return True  # docstring-only function is a placeholder
    for stmt in body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Assert):
            test = stmt.test
            if isinstance(test, ast.Constant) and bool(test.value):
                continue  # assert True / assert 1 / assert "x"
            return False
        if isinstance(stmt, ast.Raise):
            exc = stmt.exc
            target = None
            if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                target = exc.func.id
            elif isinstance(exc, ast.Name):
                target = exc.id
            if target == "NotImplementedError":
                continue
            return False
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            # Bare `NotImplementedError(...)` as a statement (no `raise`).
            call = stmt.value
            if isinstance(call.func, ast.Name) and call.func.id == "NotImplementedError":
                continue
            return False
        return False  # any other statement = real behavior
    return True


def _gate_b_lite_placeholder_tests(
    files: Optional[Mapping[str, str]],
) -> Optional[str]:
    """Gate B-lite (autonomy): reject propose_pr / update_pr if the
    files dict contains test files with placeholder-only test bodies
    OR plan docs admitting placeholder implementations.

    Catches the four unambiguous patterns Hawkman's system prompt
    explicitly rejects: ``assert True``, ``pass`` (in a ``test_*``
    function), ``NotImplementedError``, and the plan-doc admission
    string "placeholder implementations may need to be replaced".

    Uses ast.parse instead of line-grep so decorators (``@pytest.mark.X``),
    comment lines, and assertion-after-comment shapes do not bypass the
    check. A test function with a SINGLE trivial assertion FOLLOWED BY a
    real behavioral assertion still passes — the gate only fires when the
    function body is 100% trivial after the docstring.

    Returns ``None`` to pass, or a string error to abort propose_pr.
    """
    if not files:
        return None
    placeholder_tests: list[tuple[str, str]] = []  # (file, function name)
    placeholder_plans: list[str] = []
    for path, content in files.items():
        if not isinstance(path, str) or not isinstance(content, str):
            continue
        is_test_path = path.endswith(".py") and (
            "/test" in path or path.startswith("test") or "/tests/" in path
        )
        if is_test_path:
            try:
                tree = ast.parse(content)
            except SyntaxError:
                # Don't block on unparseable test files — Hawkman / CI
                # will catch the syntax error on its own and the gate
                # avoids becoming a flaky merge-blocker.
                continue
            for node in ast.walk(tree):
                if _is_placeholder_test_function(node):
                    placeholder_tests.append(
                        (path, f"def {node.name}(...)")
                    )
        if path.startswith("plans/") or path.endswith(".md"):
            if _BL_PLACEHOLDER_PLAN_DOC_RE.search(content):
                placeholder_plans.append(path)
    if not placeholder_tests and not placeholder_plans:
        return None
    parts = ["GATE_B_LITE_PLACEHOLDER:"]
    if placeholder_tests:
        parts.append(
            f" {len(placeholder_tests)} test function(s) have "
            f"placeholder-only bodies (assert True / pass / "
            f"NotImplementedError, ignoring docstrings + comments). "
            f"Hawkman GATE 4 will REQUEST_CHANGES. Add real behavioral "
            f"assertions. Examples:"
        )
        for f, snip in placeholder_tests[:3]:
            parts.append(f"  - {f}: {snip}")
    if placeholder_plans:
        parts.append(
            f" {len(placeholder_plans)} plan doc(s) contain the literal "
            f"phrase 'placeholder implementations may need to be "
            f"replaced' which Hawkman explicitly rejects on: "
            f"{placeholder_plans[:3]!r}. Remove the admission and ship "
            f"the actual implementation."
        )
    return "\n".join(parts)


def _fetch_latest_request_changes_review(
    owner: str, repo: str, pr_number: int, *, token: str
) -> Optional[str]:
    """Fetch the most recent CHANGES_REQUESTED review body for a PR.

    Returns the review body string, or ``None`` if no CHANGES_REQUESTED
    review exists OR the GH API call fails. Used by Gate E.
    """
    if not token:
        return None
    url = (
        f"https://api.github.com/repos/{owner}/{repo}/pulls/"
        f"{pr_number}/reviews?per_page=30"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "saluca-alfred/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            reviews = json.loads(r.read())
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return None
    except Exception:
        return None
    if not isinstance(reviews, list):
        return None
    rejects = [
        rv for rv in reviews
        if isinstance(rv, dict) and rv.get("state") == "CHANGES_REQUESTED"
    ]
    if not rejects:
        return None
    rejects.sort(key=lambda rv: rv.get("submitted_at") or "", reverse=True)
    return rejects[0].get("body") or None


_GATE_E_HEADING_RE = re.compile(
    r"(?im)^\#{2,}\s*"
    r"(?:addresses\s+prior\s+feedback"
    r"|prior\s+feedback"
    r"|fixes?\s+from\s+previous"
    r"|response\s+to\s+review"
    r"|review\s+response"
    r"|changes?\s+in\s+response)"
)


def _gate_e_fix_round_amnesia(
    body: Optional[str],
    *,
    pr_url: Optional[str],
    token: Optional[str] = None,
    review_fetcher: Optional[Callable[..., Optional[str]]] = None,
) -> Optional[str]:
    """Gate E (autonomy): on update_pr, the new body MUST carry an
    explicit ``## Addresses Prior Feedback`` heading (or one of the
    accepted variants) when there is a prior CHANGES_REQUESTED review.

    Earlier version (b937cae) also accepted a 10-char-or-longer keyword
    overlap with the prior review body. That heuristic admitted the
    SAL-3572 round-1 update_pr at 22:47 UTC on 2026-04-30 because the
    builder's body said "Added placeholder pytest tests" while the
    prior review flagged "placeholder test bodies" — keyword overlap
    on `placeholder` passed the gate even though the builder never
    addressed the feedback. Requiring the structural heading forces
    the builder to read the review and explicitly summarise their
    response, which is the actual behaviour the gate exists to enforce.

    Fail-open conditions:
      - No prior CHANGES_REQUESTED review on the PR
      - GH API call fails
      - pr_url is malformed
    """
    if not body or not pr_url:
        return None
    m = re.match(
        r"^https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)/?$",
        pr_url.strip(),
    )
    if not m:
        return None
    owner, repo, num_str = m.group(1), m.group(2), m.group(3)
    fetcher = review_fetcher or _fetch_latest_request_changes_review
    prior = fetcher(owner, repo, int(num_str), token=token or "")
    if not prior or len(prior.strip()) < 30:
        return None  # no actionable prior review
    if _GATE_E_HEADING_RE.search(body):
        return None
    preview = prior[:300] + ("..." if len(prior) > 300 else "")
    return (
        f"GATE_E_FIX_ROUND_AMNESIA: this fix-round update_pr body does "
        f"not carry a `## Addresses Prior Feedback` heading "
        f"(accepted variants: 'Prior Feedback', 'Fixes From Previous', "
        f"'Response to Review', 'Review Response', 'Changes in Response'). "
        f"Add a section under that heading that summarises which review "
        f"points you addressed and how. Prior review excerpt:\n\n{preview}"
    )


def _maybe_inject_apev_citation(
    body: Optional[str],
    *,
    files: Optional[Mapping[str, str]] = None,
    branch: Optional[str] = None,
    title: Optional[str] = None,
    pr_url: Optional[str] = None,
    is_fix_round: bool = False,
    linear_fetcher: Optional[Callable[[Optional[str]], Optional[str]]] = None,
) -> str:
    """Return ``body`` with a citation block appended iff one is missing.

    SAL-2953: prevents the v7y wave-1 failure mode where builders forget
    the APE/V heading and burn a hawkman review cycle on a deterministic
    template gap. Idempotent — bodies that already cite are returned
    unchanged.

    SAL-2965 changes:
      * ``is_fix_round`` (default ``False``) short-circuits the helper
        when the caller is ``update_pr``. Fix-round respawns must not
        clobber the original PR body's citation with text re-extracted
        from a possibly-drifted plan doc; the builder rewrites the body
        explicitly when needed.
      * Acceptance criteria are now sourced from the Linear ticket body
        first (canonical, what hawkman validates against), with a fall
        back to the plan-doc extraction when Linear is unreachable.
      * ``linear_fetcher`` is a test-injection seam — production callers
        leave it ``None`` to use the network-bound default.
    """
    if is_fix_round:
        # Fix-round skip: preserve whatever the original propose_pr body
        # contained. The builder owns the body on every update_pr call.
        return body or ""
    if _apev_body_has_citation(body):
        return body or ""
    ticket_code = _extract_ticket_code(branch, title, pr_url, body)
    plan_path, plan_content = _find_plan_doc_in_files(
        files or {}, ticket_code=ticket_code
    )
    # Source order: Linear ticket body (canonical) → plan-doc fallback.
    fetcher = linear_fetcher or _fetch_linear_acceptance_criteria
    acceptance = fetcher(ticket_code)
    if not acceptance:
        acceptance = _extract_acceptance_lines(plan_content)
    block = _build_apev_citation_block(
        plan_doc_path=plan_path,
        acceptance_lines=acceptance,
        ticket_code=ticket_code,
    )
    if not body:
        return block.lstrip()
    # Trim trailing whitespace before appending so the appended block
    # always starts with the canonical leading blank line.
    return body.rstrip() + block
_VALID_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")
_ALLOWED_ORGS = frozenset({"salucallc", "saluca-labs", "cristianxruvalcaba-coder"})


#: SAL-3741: pin the git binary to an absolute path so propose_pr /
#: update_pr never trip the FileNotFoundError-on-'git' bug observed
#: 2026-04-30 across SAL-3594/3595/3596/3548/3571 wave-2 dispatches.
#: Symptom: asyncio.create_subprocess_exec with cmd=[_GIT_BIN, ...] raised
#: FileNotFoundError mid-session even though /usr/bin/git existed and
#: PATH was set in env. The model misinterpreted the error as "lacks
#: git binary" and called linear_create_issue → ESCALATED → wave-gate
#: mass-excused crash. Earlier dispatches (SAL-3613/3614/3615) shipped
#: PRs fine, so the issue is environmental drift, not a permanent gap.
#: Pinning the absolute path bypasses PATH lookup entirely.
#:
#: Override via the ``ALFRED_GIT_BIN`` env var if the daemon runs on a
#: host where git lives elsewhere (e.g. /usr/local/bin/git on macOS).
_GIT_BIN = os.environ.get("ALFRED_GIT_BIN") or "/usr/bin/git"


def _git_env() -> Dict[str, str]:
    """Environment for git subprocess calls — identity + token-embedded URL support."""
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "Alfred COO Daemon")
    env.setdefault("GIT_AUTHOR_EMAIL", "alfred-coo@saluca.com")
    env.setdefault("GIT_COMMITTER_NAME", "Alfred COO Daemon")
    env.setdefault("GIT_COMMITTER_EMAIL", "alfred-coo@saluca.com")
    # SAL-3741: if PATH is somehow missing from os.environ at runtime
    # (which shouldn't happen but did empirically — see _GIT_BIN comment),
    # ensure git's directory is on the env PATH as a defensive belt.
    if "PATH" not in env or "/usr/bin" not in env.get("PATH", ""):
        env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
    return env


async def _run(
    cmd: List[str],
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> tuple[int, str, str]:
    """Await a subprocess, return (returncode, stdout, stderr). Never raises on non-zero."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        env=dict(env) if env else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return (
        proc.returncode or 0,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


def _safe_workspace_path(workspace: Path, rel_path: str) -> Optional[Path]:
    if not rel_path or not isinstance(rel_path, str):
        return None
    p = rel_path.strip().replace("\\", "/")
    if not p or p.startswith("/") or (len(p) >= 2 and p[1] == ":"):
        return None
    parts = [seg for seg in p.split("/") if seg and seg != "."]
    if not parts or any(s == ".." for s in parts):
        return None
    target = workspace / Path(*parts)
    try:
        target.resolve().relative_to(workspace.resolve())
    except ValueError:
        return None
    return target


async def propose_pr(
    owner: str,
    repo: str,
    branch: str,
    title: str,
    body: str,
    files: Dict[str, str],
    base_branch: str = "main",
    commit_message: Optional[str] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Atomic: clone → branch → write files → commit → push → open PR.

    Only repos under a known Saluca org are allowed. All file paths must be
    relative and inside the workspace — absolute paths and `..` escape are
    rejected. If any step fails the PR is not opened and the error surfaces
    in the return dict.
    """
    # SAL-2905: builder identity. Falls back to legacy GITHUB_TOKEN in
    # single-token deployments (identical behaviour to pre-2905).
    token, _id_class = _github_token_for(GitHubIdentityClass.BUILDER)
    if not token:
        return {"error": "GITHUB_TOKEN not configured"}
    if owner not in _ALLOWED_ORGS:
        return {"error": f"owner {owner!r} not in allowlist {sorted(_ALLOWED_ORGS)}"}
    if not _VALID_OWNER_RE.match(owner):
        return {"error": "invalid owner name"}
    if not _VALID_REPO_RE.match(repo):
        return {"error": "invalid repo name"}
    if not _VALID_BRANCH_RE.match(branch):
        return {"error": "invalid branch name"}
    if not _VALID_BRANCH_RE.match(base_branch):
        return {"error": "invalid base_branch name"}
    if not isinstance(files, dict) or not files:
        return {"error": "files must be a non-empty dict of relpath -> content"}

    workspace_id = task_id or get_current_task_id() or f"ad-hoc-{os.getpid()}"
    workspace = WORKSPACE_ROOT / workspace_id / repo
    # Fresh clone for determinism. If re-running the same task, wipe prior state.
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    workspace.parent.mkdir(parents=True, exist_ok=True)

    clone_url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"

    env = _git_env()

    rc, out, err = await _run(
        [_GIT_BIN, "clone", "--depth", "50", "--branch", base_branch, clone_url, str(workspace)],
        env=env,
    )
    if rc != 0:
        return {"error": "git clone failed", "stderr": err[:500]}

    rc, _, err = await _run(
        [_GIT_BIN, "checkout", "-B", branch], cwd=workspace, env=env,
    )
    if rc != 0:
        return {"error": "git checkout -B failed", "stderr": err[:500]}

    written: List[str] = []
    for rel_path, content in files.items():
        target = _safe_workspace_path(workspace, rel_path)
        if target is None:
            return {"error": f"unsafe path: {rel_path!r}"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        written.append(rel_path)

    rc, _, err = await _run([_GIT_BIN, "add", *written], cwd=workspace, env=env)
    if rc != 0:
        return {"error": "git add failed", "stderr": err[:500]}

    msg = commit_message or f"{title}\n\nAuthored by Alfred COO daemon."
    rc, _, err = await _run(
        [_GIT_BIN, "commit", "-m", msg], cwd=workspace, env=env,
    )
    if rc != 0:
        return {"error": "git commit failed", "stderr": err[:500]}

    rc, _, err = await _run(
        [_GIT_BIN, "push", "-u", "origin", branch], cwd=workspace, env=env,
    )
    if rc != 0:
        return {"error": "git push failed", "stderr": err[:500]}

    # SAL-2953: deterministically auto-inject the `## APE/V Citation` block
    # if the builder's body is missing it. Hawkman's GATE 1 REQUEST_CHANGES
    # every PR without one (see persona.py); v7y wave-1 burned 3 review
    # cycles on 2 tickets purely because builders forgot the heading. The
    # plan doc the builder must ship in the same `files` dict carries the
    # verbatim acceptance criteria (persona Step 4(a)), so we synthesise
    # the block from artifacts already in this call. Idempotent — bodies
    # that already cite are returned unchanged.
    body = _maybe_inject_apev_citation(
        body, files=files, branch=branch, title=title
    )

    # Gate A (autonomy gate): verify the canonical Linear APE/V section
    # is byte-equal in the PR body. Auto-inject above only fires when
    # the heading is *absent*; paraphrased headings escape it AND fail
    # Hawkman GATE 1. Catching here saves a full review cycle per PR.
    # Fail-open on infra issues — see _gate_a_apev_byte_match.
    gate_a_err = _gate_a_apev_byte_match(
        body, branch=branch, title=title, files=files,
    )
    if gate_a_err:
        return {"error": gate_a_err}

    # Gate D (autonomy gate): if the PR adds both router files and test
    # files, every URL the tests call must be mounted by some route in
    # the same files dict. Catches the soul-svc PR #66 cycle-3 pattern
    # where tests called /v1/mssp/audit but the router mounted at
    # /v1/mssp/consent/audit. Fail-open when only one side is in scope.
    gate_d_err = _gate_d_endpoint_path_consistency(files)
    if gate_d_err:
        return {"error": gate_d_err}

    # Gate B-lite (autonomy gate): reject if any test file has a 100%
    # placeholder body (assert True / pass / NotImplementedError) or
    # any plan doc admits "placeholder implementations may need to be
    # replaced" — Hawkman explicitly REQUEST_CHANGES on these.
    gate_b_err = _gate_b_lite_placeholder_tests(files)
    if gate_b_err:
        return {"error": gate_b_err}

    # Open the PR via GitHub REST API (avoids gh CLI, which needs a writable
    # $HOME for its config — the daemon runs with systemd ProtectHome=true).
    pr_payload = json.dumps({
        "title": title,
        "body": body or "(no body)",
        "head": branch,
        "base": base_branch,
    }).encode()
    pr_req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/pulls",
        data=pr_payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "saluca-alfred/1.0",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(pr_req, timeout=30) as r:
            pr_body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {
            "error": f"github pulls http {e.code}",
            "response": e.read().decode()[:500],
        }
    except Exception as e:
        return {"error": f"github api transport: {type(e).__name__}: {e}"}

    return {
        "pr_url": pr_body.get("html_url"),
        "pr_number": pr_body.get("number"),
        "branch": branch,
        "files_written": written,
        "commit_message": msg.split("\n")[0],
    }


# ── update_pr (AB-17-o) ─────────────────────────────────────────────────────
#
# Fix-round companion to propose_pr. When hawkman-qa-a emits REQUEST_CHANGES
# on an existing PR, the orchestrator respawns a child alfred-coo-a task with
# a ``## Prior PR`` section pinning the branch. The child must push fixes to
# that *existing* branch so the original PR and its review thread are
# preserved. Using ``propose_pr`` on a respawn would open a duplicate PR on a
# new branch, which is the exact behaviour v8-full-v4 exposed on wave-0
# (acs#59/60, ts#4/5, ss#17/18). Worst case with MAX_REVIEW_CYCLES=3 across
# 95 tickets would be 285 orphan PRs. This tool exists to stop that bleed.

_PR_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)/?$"
)


async def update_pr(
    pr_url: str,
    branch: str,
    commit_message: str,
    files: Optional[List[Dict[str, str]]] = None,
    title: Optional[str] = None,
    body: Optional[str] = None,
    force_push: bool = False,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Push file updates to an existing PR's feature branch.

    AB-17-o (2026-04-24): companion to ``propose_pr`` for fix-round respawns
    after a hawkman REQUEST_CHANGES. Clones the repo, fetches the existing
    branch, writes the given files, commits, and pushes. Preserves the
    existing PR URL + review thread instead of opening a duplicate PR on a
    fresh timestamped branch.

    ``files`` is a list of ``{"path": str, "content": str}`` dicts. The list
    form (versus propose_pr's dict form) matches how hawkman feedback names
    the paths to edit and avoids key-collision edge cases when the fix-round
    spec carries ordered edits.

    Errors raise ``UpdatePrError`` so the dispatch loop surfaces a clean
    message rather than a partial push. In particular: refuse to touch a
    CLOSED or MERGED PR, refuse a missing branch (that is propose_pr's job),
    refuse an empty ``files`` list (silent no-op would hide a bug), and
    refuse a non-fast-forward push unless ``force_push=True``.

    Returns ``{"pushed_sha", "pr_url", "commit_url", "branch", "pr_number",
    "files_written", "commit_message"}``.
    """
    # SAL-2905: builder identity. update_pr is a fix-round on an
    # already-open PR; the push must come from the same identity that
    # opened the PR or hawkman's re-review will see a different commit
    # author than PR author.
    token, _id_class = _github_token_for(GitHubIdentityClass.BUILDER)
    if not token:
        return {"error": "GITHUB_TOKEN not configured"}

    # Parse pr_url into (owner, repo, pr_number).
    m = _PR_URL_PATTERN.match((pr_url or "").strip())
    if not m:
        return {
            "error": (
                f"invalid pr_url {pr_url!r}; expected "
                "https://github.com/<owner>/<repo>/pull/<n>"
            )
        }
    owner = m.group("owner")
    repo = m.group("repo")
    try:
        pr_number = int(m.group("number"))
    except (TypeError, ValueError):
        return {"error": "could not parse pr number from url"}

    if owner not in _ALLOWED_ORGS:
        return {"error": f"owner {owner!r} not in allowlist {sorted(_ALLOWED_ORGS)}"}
    if not _VALID_OWNER_RE.match(owner):
        return {"error": "invalid owner name"}
    if not _VALID_REPO_RE.match(repo):
        return {"error": "invalid repo name"}
    if not _VALID_BRANCH_RE.match(branch or ""):
        return {"error": "invalid branch name"}
    if not commit_message or not commit_message.strip():
        return {"error": "commit_message must be non-empty"}

    if not isinstance(files, list) or not files:
        return {"error": "files must be a non-empty list of {path, content}"}
    for entry in files:
        if not isinstance(entry, dict):
            return {"error": "each files[] entry must be a dict"}
        if "path" not in entry or "content" not in entry:
            return {"error": "each files[] entry must have 'path' and 'content'"}
        if not isinstance(entry["path"], str) or not isinstance(entry["content"], str):
            return {"error": "files[] path and content must be strings"}

    # ── Step 1: confirm PR is open. Refuse closed / merged up-front so the
    # caller gets a clear error instead of a mystery push against a stale
    # branch that nobody is reading anymore.
    pr_meta_req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "saluca-alfred/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(pr_meta_req, timeout=30) as r:
            pr_meta = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {
            "error": f"github pulls GET http {e.code}",
            "response": e.read().decode()[:500],
        }
    except Exception as e:
        return {"error": f"github api transport: {type(e).__name__}: {e}"}

    state = (pr_meta.get("state") or "").lower()
    merged = bool(pr_meta.get("merged"))
    if merged:
        return {"error": "PR not open: state=merged"}
    if state != "open":
        return {"error": f"PR not open: state={state}"}

    head_ref = (pr_meta.get("head") or {}).get("ref") or ""
    if head_ref and head_ref != branch:
        # Caller named a branch that doesn't match the PR head. Bail rather
        # than push to the wrong branch and confuse the review thread.
        return {
            "error": (
                f"branch mismatch: pr head is {head_ref!r} but caller "
                f"passed {branch!r}"
            )
        }

    # ── Step 2: clone fresh workspace + fetch the feature branch. We clone
    # main shallowly (--no-checkout) then fetch + check out the target
    # branch. This avoids needing the default branch at all and keeps the
    # operation fast on large repos.
    workspace_id = task_id or get_current_task_id() or f"ad-hoc-{os.getpid()}"
    workspace = WORKSPACE_ROOT / workspace_id / f"{repo}-update-{pr_number}"
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    workspace.parent.mkdir(parents=True, exist_ok=True)

    clone_url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
    env = _git_env()

    rc, _, err = await _run(
        [_GIT_BIN, "clone", "--no-checkout", "--filter=blob:none", clone_url, str(workspace)],
        env=env,
    )
    if rc != 0:
        return {"error": "git clone failed", "stderr": err[:500]}

    rc, _, err = await _run(
        [_GIT_BIN, "fetch", "origin", branch], cwd=workspace, env=env,
    )
    if rc != 0:
        return {
            "error": f"branch not found: {branch}",
            "stderr": err[:500],
        }

    rc, _, err = await _run(
        [_GIT_BIN, "checkout", "-B", branch, f"origin/{branch}"],
        cwd=workspace, env=env,
    )
    if rc != 0:
        return {"error": "git checkout failed", "stderr": err[:500]}

    # ── Step 3: write files + commit.
    written: List[str] = []
    for entry in files:
        rel_path = entry["path"]
        content = entry["content"]
        target = _safe_workspace_path(workspace, rel_path)
        if target is None:
            return {"error": f"unsafe path: {rel_path!r}"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        written.append(rel_path)

    rc, _, err = await _run([_GIT_BIN, "add", *written], cwd=workspace, env=env)
    if rc != 0:
        return {"error": "git add failed", "stderr": err[:500]}

    rc, _, err = await _run(
        [_GIT_BIN, "commit", "-m", commit_message], cwd=workspace, env=env,
    )
    if rc != 0:
        # "nothing to commit" is a distinct failure — the caller passed files
        # identical to what is already on the branch, which shouldn't silently
        # succeed. Surface the git stderr verbatim (truncated) so the model
        # can adjust.
        return {"error": "git commit failed", "stderr": err[:500]}

    push_cmd = [_GIT_BIN, "push", "origin", branch]
    if force_push:
        push_cmd = [_GIT_BIN, "push", "--force-with-lease", "origin", branch]
    rc, _, err = await _run(push_cmd, cwd=workspace, env=env)
    if rc != 0:
        hint = (
            " (non-fast-forward; retry with force_push=True if intentional)"
            if "non-fast-forward" in (err or "").lower()
            or "rejected" in (err or "").lower()
            else ""
        )
        return {
            "error": f"git push failed{hint}",
            "stderr": err[:500],
        }

    # Capture the pushed sha for the return envelope.
    rc, sha_out, err = await _run(
        [_GIT_BIN, "rev-parse", "HEAD"], cwd=workspace, env=env,
    )
    pushed_sha = (sha_out or "").strip()
    if rc != 0 or not pushed_sha:
        return {"error": "could not read pushed sha", "stderr": err[:500]}

    # ── Step 4: optional PR title / body update.
    if title is not None or body is not None:
        # SAL-2953/SAL-2965: on fix-round respawn we deliberately skip the
        # auto-inject. The original propose_pr already wrote a citation
        # block (either builder-authored or auto-injected from Linear);
        # re-running the inject here would re-extract from a possibly
        # drifted plan doc and clobber a previously-good citation with
        # the fix-round directive. The builder owns the body on update_pr.
        if body is not None:
            body = _maybe_inject_apev_citation(
                body,
                branch=branch,
                title=title,
                pr_url=pr_url,
                is_fix_round=True,
            )
            # Gate A on update_pr: the fix-round MUST still carry a
            # byte-equal APE/V citation. update_pr is allowed to skip
            # auto-inject (so the builder owns the body on respawn) but
            # MUST NOT lose the citation between rounds — that's exactly
            # the cycle-3-still-missing pattern from soul-svc PR #66.
            gate_a_err = _gate_a_apev_byte_match(
                body, branch=branch, title=title, pr_url=pr_url,
            )
            if gate_a_err:
                return {"error": gate_a_err}

            # Gate E (autonomy gate): on a fix-round update, the new
            # body MUST acknowledge the prior CHANGES_REQUESTED review,
            # either via a `## Addresses Prior Feedback` heading or by
            # citing a 10+-char phrase from the prior review. Catches
            # the cycle-2-3 amnesia pattern where the fix-round builder
            # ignores the prior review and re-trips the same gate.
            gate_e_err = _gate_e_fix_round_amnesia(
                body, pr_url=pr_url, token=token,
            )
            if gate_e_err:
                return {"error": gate_e_err}

        # Gate D + B-lite on update_pr: same checks the fresh PR path
        # uses. update_pr's files are list-of-dicts; flatten to
        # {path: content} for the gates to consume.
        if files:
            files_dict_for_gates = {
                e["path"]: e["content"]
                for e in files
                if isinstance(e, dict) and "path" in e and "content" in e
            }
            gate_d_err = _gate_d_endpoint_path_consistency(
                files_dict_for_gates,
            )
            if gate_d_err:
                return {"error": gate_d_err}
            gate_b_err = _gate_b_lite_placeholder_tests(
                files_dict_for_gates,
            )
            if gate_b_err:
                return {"error": gate_b_err}

        patch_payload: Dict[str, Any] = {}
        if title is not None:
            patch_payload["title"] = str(title)
        if body is not None:
            patch_payload["body"] = str(body)
        patch_req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            data=json.dumps(patch_payload).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "saluca-alfred/1.0",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="PATCH",
        )
        try:
            with urllib.request.urlopen(patch_req, timeout=30) as r:
                r.read()  # consume
        except urllib.error.HTTPError as e:
            # Push already succeeded, so don't fail the whole call — return
            # the sha + a warning. The fix-round is still landed.
            return {
                "pushed_sha": pushed_sha,
                "pr_url": pr_url,
                "pr_number": pr_number,
                "branch": branch,
                "files_written": written,
                "commit_message": commit_message.split("\n")[0],
                "commit_url": f"https://github.com/{owner}/{repo}/commit/{pushed_sha}",
                "warning": (
                    f"pr title/body patch failed http {e.code}: "
                    f"{e.read().decode()[:200]}"
                ),
            }
        except Exception as e:
            return {
                "pushed_sha": pushed_sha,
                "pr_url": pr_url,
                "pr_number": pr_number,
                "branch": branch,
                "files_written": written,
                "commit_message": commit_message.split("\n")[0],
                "commit_url": f"https://github.com/{owner}/{repo}/commit/{pushed_sha}",
                "warning": f"pr patch transport: {type(e).__name__}: {e}",
            }

    return {
        "pushed_sha": pushed_sha,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "branch": branch,
        "files_written": written,
        "commit_message": commit_message.split("\n")[0],
        "commit_url": f"https://github.com/{owner}/{repo}/commit/{pushed_sha}",
    }


# ── pr_review ───────────────────────────────────────────────────────────────

_PR_REVIEW_EVENTS = frozenset({"APPROVE", "REQUEST_CHANGES", "COMMENT"})


async def pr_review(
    owner: str,
    repo: str,
    pr_number: int,
    event: str,
    body: str,
    line_comments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Submit a pull-request review on a Saluca-owned repo.

    Posts to https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews
    with {body, event, comments}. `event` must be one of APPROVE,
    REQUEST_CHANGES, or COMMENT. `line_comments` is an optional list of
    {"path", "line", "body"} dicts (GitHub review-comment schema). Returns
    {review_id, state, submitted_at, html_url} on success.
    """
    # SAL-2905: QA identity. With GITHUB_TOKEN_QA set, GitHub's
    # /reviews endpoint stops 422-ing on builder-authored PRs and
    # the self-authored fallback in _post_pr_comment never fires.
    token, _id_class = _github_token_for(GitHubIdentityClass.QA)
    if not token:
        return {"error": "GITHUB_TOKEN not configured"}
    if owner not in _ALLOWED_ORGS:
        return {"error": f"owner {owner!r} not in allowlist {sorted(_ALLOWED_ORGS)}"}
    if not _VALID_OWNER_RE.match(owner):
        return {"error": "invalid owner name"}
    if not _VALID_REPO_RE.match(repo):
        return {"error": "invalid repo name"}
    if event not in _PR_REVIEW_EVENTS:
        return {"error": f"event {event!r} not in {sorted(_PR_REVIEW_EVENTS)}"}
    try:
        pr_num = int(pr_number)
    except (TypeError, ValueError):
        return {"error": "pr_number must be an integer"}
    if pr_num <= 0:
        return {"error": "pr_number must be positive"}

    comments = line_comments or []
    if not isinstance(comments, list):
        return {"error": "line_comments must be a list"}
    for c in comments:
        if not isinstance(c, dict):
            return {"error": "each line_comment must be a dict"}
        if not all(k in c for k in ("path", "line", "body")):
            return {"error": "each line_comment must have 'path', 'line', 'body'"}

    payload = json.dumps({
        "body": body or "",
        "event": event,
        "comments": comments,
    }).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_num}/reviews",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "saluca-alfred/1.0",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")[:800]
        # GitHub 422 on self-authored PRs: fall back to posting the review as a
        # PR comment so the analysis still lands in a visible place. SAL-2905
        # adds split-identity routing (this handler now uses GITHUB_TOKEN_QA
        # when set), so this fallback only fires in legacy single-token
        # deployments. The fallback is retained for backwards compat.
        if e.code == 422 and "own pull request" in err_body.lower():
            comment_result = await _post_pr_comment(
                owner, repo, pr_num, token,
                event=event, body=body or "(empty review body)",
                line_comments=comments,
            )
            if "error" not in comment_result:
                return {
                    "state": "COMMENTED_FALLBACK",
                    "review_id": None,
                    "fallback_reason": "self-authored PR; used issue-comment",
                    "html_url": comment_result.get("html_url"),
                    "comment_id": comment_result.get("comment_id"),
                    "intended_event": event,
                }
            return {
                "error": f"github reviews http {e.code} (fallback also failed)",
                "response": err_body,
                "fallback_error": comment_result.get("error"),
            }
        return {"error": f"github reviews http {e.code}", "response": err_body}
    except Exception as e:
        return {"error": f"github api transport: {type(e).__name__}: {e}"}

    return {
        "review_id": resp.get("id"),
        "state": resp.get("state"),
        "submitted_at": resp.get("submitted_at"),
        "html_url": resp.get("html_url"),
    }


async def _post_pr_comment(
    owner: str,
    repo: str,
    pr_num: int,
    token: str,
    event: str,
    body: str,
    line_comments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Post a PR comment via the issues API. pr_review self-authored fallback."""
    prefix = {
        "APPROVE": "### :white_check_mark: Review: APPROVE (fallback - self-authored PR)",
        "REQUEST_CHANGES": "### :warning: Review: REQUEST_CHANGES (fallback - self-authored PR)",
        "COMMENT": "### Review: COMMENT (fallback - self-authored PR)",
    }.get(event, f"### Review: {event}")

    full_body = f"{prefix}\n\n{body}"
    if line_comments:
        full_body += "\n\n---\n#### Line comments"
        for c in line_comments:
            full_body += f"\n- `{c.get('path')}:{c.get('line')}` - {c.get('body')}"

    payload = json.dumps({"body": full_body}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_num}/comments",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "saluca-alfred/1.0",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": f"comment http {e.code}", "response": e.read().decode(errors="replace")[:400]}
    except Exception as e:
        return {"error": f"comment transport: {type(e).__name__}: {e}"}
    return {"comment_id": resp.get("id"), "html_url": resp.get("html_url")}


# ── pr_files_get ────────────────────────────────────────────────────────────

# Caps for pr_files_get payloads. The goal is to keep a single tool call under
# a few hundred KB while still returning useful review surface. PRs with more
# files or larger individual files are truncated with explicit markers so the
# model knows to call out the gap rather than silently missing coverage.
PR_FILES_GET_MAX_FILES = 50
PR_FILES_GET_MAX_CONTENT_BYTES = 20 * 1024  # 20 KB per file
PR_FILES_GET_TIMEOUT_SEC = 30.0


async def _github_api_get_json(url: str, token: str) -> tuple[Optional[Any], Optional[str]]:
    """GET a GitHub REST endpoint with auth. Returns (body, error-string)."""
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "alfred-coo-svc",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=PR_FILES_GET_TIMEOUT_SEC) as r:
            return json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        return None, f"github http {e.code}: {e.read().decode(errors='replace')[:300]}"
    except Exception as e:  # pragma: no cover — defensive
        return None, f"github transport: {type(e).__name__}: {e}"


async def pr_files_get(
    owner: str,
    repo: str,
    pr_number: int,
) -> Dict[str, Any]:
    """Fetch all files in a PR with their current content at head SHA.

    Authenticated against api.github.com via GITHUB_TOKEN. Works on private
    repos in the allowlisted orgs. Single tool call replaces ~10+ http_get
    calls a QA persona would otherwise need to walk a PR.
    """
    # SAL-2905: QA identity. Read-only, but keeping the audit trail
    # cohesive ("hawkman fetched these files" not "the daemon"
    # account fetched).
    token, _id_class = _github_token_for(GitHubIdentityClass.QA)
    if not token:
        return {"error": "GITHUB_TOKEN not set"}
    if owner not in _ALLOWED_ORGS:
        return {"error": f"owner {owner!r} not in allowlist {sorted(_ALLOWED_ORGS)}"}
    if not _VALID_OWNER_RE.match(owner):
        return {"error": "invalid owner name"}
    if not _VALID_REPO_RE.match(repo):
        return {"error": "invalid repo name"}
    try:
        pr_num = int(pr_number)
    except (TypeError, ValueError):
        return {"error": "pr_number must be an integer"}
    if pr_num <= 0:
        return {"error": "pr_number must be positive"}

    # 1. PR metadata for head SHA + branch refs.
    pr_meta, err = await _github_api_get_json(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_num}",
        token,
    )
    if err is not None:
        return {"error": f"pr metadata fetch failed: {err}"}
    head = (pr_meta or {}).get("head") or {}
    base = (pr_meta or {}).get("base") or {}
    head_sha = head.get("sha")
    head_ref = head.get("ref")
    base_ref = base.get("ref")
    if not head_sha:
        return {"error": "pr metadata missing head.sha"}

    # 2. Files list (GitHub paginates at 100 per page; we cap at the first page
    # plus a truncation marker). Sorted by GitHub in commit-diff order.
    files_list, err = await _github_api_get_json(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_num}/files?per_page=100",
        token,
    )
    if err is not None:
        return {"error": f"pr files fetch failed: {err}"}
    if not isinstance(files_list, list):
        return {"error": "pr files response was not a list"}

    total_files = len(files_list)
    truncated = total_files > PR_FILES_GET_MAX_FILES
    files_slice = files_list[:PR_FILES_GET_MAX_FILES]

    # 3. For each non-removed file, fetch raw contents at head SHA.
    out_files: List[Dict[str, Any]] = []
    for f in files_slice:
        path = f.get("filename")
        status = f.get("status")
        entry: Dict[str, Any] = {
            "path": path,
            "status": status,
            "additions": f.get("additions"),
            "deletions": f.get("deletions"),
        }
        if status == "removed":
            out_files.append(entry)
            continue
        if not path:
            entry["content_error"] = "missing filename in PR files response"
            out_files.append(entry)
            continue

        contents_url = (
            f"https://api.github.com/repos/{owner}/{repo}/contents/"
            f"{urllib.request.quote(path)}?ref={head_sha}"
        )
        body, err = await _github_api_get_json(contents_url, token)
        if err is not None:
            entry["content_error"] = err
            out_files.append(entry)
            continue
        if not isinstance(body, dict):
            entry["content_error"] = "contents response was not an object"
            out_files.append(entry)
            continue
        encoding = body.get("encoding")
        raw = body.get("content") or ""
        if encoding == "base64":
            try:
                decoded = base64.b64decode(raw, validate=False)
            except Exception as e:
                entry["content_error"] = f"base64 decode failed: {type(e).__name__}: {e}"
                out_files.append(entry)
                continue
        elif encoding in (None, "", "utf-8", "none"):
            decoded = raw.encode("utf-8", errors="replace") if isinstance(raw, str) else b""
        else:
            entry["content_error"] = f"unsupported encoding: {encoding}"
            out_files.append(entry)
            continue

        full_len = len(decoded)
        if full_len > PR_FILES_GET_MAX_CONTENT_BYTES:
            clipped = decoded[:PR_FILES_GET_MAX_CONTENT_BYTES]
            text = clipped.decode("utf-8", errors="replace")
            dropped = full_len - PR_FILES_GET_MAX_CONTENT_BYTES
            entry["content"] = text + f"\n...[truncated {dropped} bytes]"
            entry["content_truncated"] = True
            entry["content_bytes_total"] = full_len
        else:
            entry["content"] = decoded.decode("utf-8", errors="replace")
            entry["content_truncated"] = False
            entry["content_bytes_total"] = full_len

        out_files.append(entry)

    return {
        "pr_number": pr_num,
        "head_sha": head_sha,
        "head": head_ref,
        "base": base_ref,
        "files": out_files,
        "truncated": truncated,
        "total_files": total_files,
    }


# ── github_merge_pr ──────────────────────────────────────────────────────────────

_PR_MERGE_METHODS = frozenset({"squash", "merge", "rebase"})


async def github_merge_pr(
    owner: str,
    repo: str,
    pr_number: int,
    merge_method: str = "squash",
    commit_title: Optional[str] = None,
    commit_message: Optional[str] = None,
) -> Dict[str, Any]:
    """Merge a pull request on a Saluca-owned repo.

    Posts PUT https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/merge
    with `{merge_method, commit_title?, commit_message?}`. `merge_method` must
    be one of squash, merge, rebase. Used by the autonomous_build orchestrator
    after a QA persona has landed an APPROVE review. Only Saluca-owned orgs
    are allowed. Returns `{ok, merged, sha, message}` on success; structured
    error dict on 405 (not mergeable), 409 (stale head), 422 (unprocessable),
    or other failure.
    """
    # SAL-2905: orchestrator identity. Falls back to QA token if
    # GITHUB_TOKEN_ORCHESTRATOR is unset (semantic: "QA approved →
    # QA merges"); falls back to legacy GITHUB_TOKEN if neither is
    # set.
    token, _id_class = _github_token_for(GitHubIdentityClass.ORCHESTRATOR)
    if not token:
        return {"error": "missing GITHUB_TOKEN"}
    if owner not in _ALLOWED_ORGS:
        return {"error": f"owner {owner!r} not in allowlist {sorted(_ALLOWED_ORGS)}"}
    if not _VALID_OWNER_RE.match(owner):
        return {"error": "invalid owner name"}
    if not _VALID_REPO_RE.match(repo):
        return {"error": "invalid repo name"}
    if merge_method not in _PR_MERGE_METHODS:
        return {
            "error": f"merge_method {merge_method!r} not in {sorted(_PR_MERGE_METHODS)}"
        }
    try:
        pr_num = int(pr_number)
    except (TypeError, ValueError):
        return {"error": "pr_number must be an integer"}
    if pr_num <= 0:
        return {"error": "pr_number must be positive"}

    body: Dict[str, Any] = {"merge_method": merge_method}
    if commit_title is not None:
        body["commit_title"] = commit_title
    if commit_message is not None:
        body["commit_message"] = commit_message

    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_num}/merge",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "saluca-alfred/1.0",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        err_text = e.read().decode(errors="replace")[:500]
        if e.code == 405:
            return {"error": "not_mergeable", "status": 405, "body": err_text}
        if e.code == 409:
            return {"error": "stale_head", "status": 409, "body": err_text}
        if e.code == 422:
            # Surface GitHub's own message when it's JSON; otherwise the raw body.
            try:
                err_json = json.loads(err_text)
            except ValueError:
                err_json = None
            msg = None
            if isinstance(err_json, dict):
                msg = err_json.get("message")
            return {
                "error": msg or "unprocessable",
                "status": 422,
                "body": err_json if err_json is not None else err_text,
            }
        return {"error": f"github merge http {e.code}", "status": e.code, "body": err_text}
    except Exception as e:
        return {"error": f"github api transport: {type(e).__name__}: {e}"}

    return {
        "ok": True,
        "merged": bool(resp.get("merged", True)),
        "sha": resp.get("sha"),
        "message": resp.get("message"),
    }


# ── http_get ────────────────────────────────────────────────────────────────

# Maximum body bytes we'll read into a tool result. Larger responses are
# truncated with a marker — the model gets the head + a note. Keeps tool
# results from blowing up the context window.
HTTP_GET_MAX_BYTES = 256 * 1024  # 256 KB
HTTP_GET_TIMEOUT_SEC = 15.0

# Content types we hand back as text. Anything else is rejected — we don't
# want the model to see base64 binaries or binary blobs.
_ALLOWED_CONTENT_TYPE_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
)


def _is_allowed_http_url(url: str) -> tuple[bool, str]:
    """Strict allowlist for http_get. Returns (ok, reason-if-not)."""
    if not url or not isinstance(url, str):
        return False, "url must be a non-empty string"
    if not (url.startswith("http://") or url.startswith("https://")):
        return False, "only http:// and https:// schemes are allowed"
    # Parse host + path without the stdlib dep (keeps this pure).
    scheme_sep = url.index("://") + 3
    path_sep = url.find("/", scheme_sep)
    host_port = url[scheme_sep:path_sep] if path_sep != -1 else url[scheme_sep:]
    path = url[path_sep:] if path_sep != -1 else "/"
    # Strip user@ and :port; normalise lowercase host.
    if "@" in host_port:
        host_port = host_port.split("@", 1)[1]
    host = host_port.split(":", 1)[0].lower()
    if not host:
        return False, "empty host"

    # GitHub sources — restrict to Saluca-owned paths.
    if host == "github.com":
        for prefix in ("/salucallc/", "/saluca-labs/", "/cristianxruvalcaba-coder/"):
            if path.startswith(prefix):
                return True, ""
        return False, f"github.com path not in Saluca allowlist: {path[:60]}"
    if host == "raw.githubusercontent.com":
        for prefix in ("/salucallc/", "/saluca-labs/", "/cristianxruvalcaba-coder/"):
            if path.startswith(prefix):
                return True, ""
        return False, f"raw.githubusercontent.com path not in Saluca allowlist: {path[:60]}"
    if host == "api.github.com":
        return True, ""  # token-gated by GitHub itself; we pass no auth, so read-only public

    # Saluca-owned domains — any subdomain.
    for suffix in (".saluca.com", ".tiresias.network", ".asphodel.ai"):
        if host == suffix[1:] or host.endswith(suffix):
            return True, ""

    # Research + docs — narrow list, expand later if needed.
    if host in {
        "arxiv.org",
        "www.arxiv.org",
        "docs.anthropic.com",
        "docs.python.org",
        "docs.github.com",
    }:
        return True, ""

    return False, f"host {host!r} not in allowlist"


_GITHUB_AUTH_HOSTS = frozenset({
    "api.github.com",
    "raw.githubusercontent.com",
    "github.com",
    "codeload.github.com",
})


def _github_authed_url(url: str) -> bool:
    """True if url targets a GitHub host that accepts/requires token auth."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    return host in _GITHUB_AUTH_HOSTS


async def http_get(url: str) -> Dict[str, Any]:
    """Read-only GET against an allowlisted URL.

    Returns {status, headers, body, truncated, bytes_read}. The body is clamped
    to HTTP_GET_MAX_BYTES; larger responses arrive truncated with an explicit
    marker. Only text-ish content types are accepted.

    Auth: if the target host is a GitHub endpoint (api.github.com,
    raw.githubusercontent.com, github.com) and GITHUB_TOKEN is set,
    an Authorization bearer header is attached. This lets QA/review personas
    read private repo contents inside the allowlisted Saluca orgs. The
    allowlist check in `_is_allowed_http_url` still bounds which orgs/paths
    are reachable.
    """
    ok, reason = _is_allowed_http_url(url)
    if not ok:
        return {"error": f"url rejected: {reason}"}

    headers = {
        "User-Agent": "saluca-alfred/1.0 (http_get tool)",
        "Accept": "text/*, application/json;q=0.9, */*;q=0.1",
    }
    if _github_authed_url(url):
        # SAL-2905: route by current persona — builder personas read
        # repos for grounding, QA personas read external spec docs.
        # Falls back to legacy GITHUB_TOKEN if no per-persona token
        # is set or no persona is active.
        token, _id_class = token_for_persona(get_current_persona())
        if not token:
            # Final legacy fallback for ad-hoc / un-personaed callers.
            token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
            headers["Accept"] = "application/vnd.github+json, text/*, */*;q=0.1"

    req = urllib.request.Request(
        url,
        headers=headers,
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_GET_TIMEOUT_SEC) as resp:
            status = resp.status
            ctype = (resp.headers.get("Content-Type") or "").lower()
            # Guard against binary blobs — even inside the allowlist some endpoints serve PDFs.
            if not any(ctype.startswith(p) for p in _ALLOWED_CONTENT_TYPE_PREFIXES):
                return {
                    "error": f"content-type not allowed: {ctype or '(missing)'}",
                    "status": status,
                }
            body_bytes = resp.read(HTTP_GET_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        return {"error": f"http {e.code}", "response": e.read().decode(errors="replace")[:500]}
    except Exception as e:
        return {"error": f"transport: {type(e).__name__}: {e}"}

    truncated = len(body_bytes) > HTTP_GET_MAX_BYTES
    if truncated:
        body_bytes = body_bytes[:HTTP_GET_MAX_BYTES]
    body = body_bytes.decode("utf-8", errors="replace")
    if truncated:
        body += "\n\n[... response truncated at {} bytes ...]".format(HTTP_GET_MAX_BYTES)

    return {
        "status": status,
        "content_type": ctype,
        "body": body,
        "truncated": truncated,
        "bytes_read": len(body_bytes),
    }


# ── slack_ack_poll ──────────────────────────────────────────────────────────

SLACK_ACK_POLL_TIMEOUT_SEC = 30.0
SLACK_ACK_POLL_PAGE_LIMIT = 100

# Relaxed-matcher token set (Fix E). Applied when (a) the message is a
# threaded reply to the gate post or (b) only one gate is currently
# pending. These shortened forms accept the natural ways Cristian replies
# without the literal SS-08 token: "approved", "lgtm", a thumbs-up emoji,
# the canonical Slack `:thumbsup:` shortcode, or a plain `+1`. Matched
# case-insensitive as full-token regex (anchored to word/punctuation
# boundaries so "lgtm-but-no" doesn't false-positive).
RELAXED_ACK_TOKEN_REGEXES: List[str] = [
    r"\back(?:nowledged)?\b",
    r"\bapprove(?:d)?\b",
    r"\blgtm\b",
    r"\bok(?:ay)?\b",
    r"\bgo\b",
    r"\bship\s*it\b",
    r"\+1",
    r"👍",
    r":thumbsup:",
    r":\+1:",
    r":white_check_mark:",
    r"✅",
]


def _compile_relaxed_patterns() -> List[tuple[str, "re.Pattern[str]"]]:
    out: List[tuple[str, "re.Pattern[str]"]] = []
    for kw in RELAXED_ACK_TOKEN_REGEXES:
        try:
            out.append((kw, re.compile(kw, re.IGNORECASE)))
        except re.error:
            # Skip malformed entries rather than failing the whole poll;
            # the constant is hand-curated so this should never fire in
            # production.
            continue
    return out


async def slack_ack_poll(
    channel: str,
    after_ts: str,
    author_user_id: str,
    keywords: List[str],
    *,
    gate_post_ts: Optional[str] = None,
    relaxed: bool = False,
    single_pending: bool = False,
) -> Dict[str, Any]:
    """Poll Slack `conversations.history` for an ACK message from one author.

    Returns the FIRST matching message (case-insensitive regex on `keywords`)
    from `author_user_id` posted after `after_ts`. Paginates via cursor until
    the full history slice (from `after_ts` forward) is exhausted or a match
    is found.

    Used by the autonomous_build orchestrator's SS-08 gate: post the claims
    schema to #batcave, wait for Cristian to reply `ACK SS-08` (or similar),
    then proceed with dispatch.

    Fix E (relaxed matcher, default off):
      * ``relaxed=True`` opts into the shortened-token set
        (``RELAXED_ACK_TOKEN_REGEXES``) under two safety conditions:
        (a) the message is a threaded reply to ``gate_post_ts`` — the
            thread context implies the ACK target — or
        (b) ``single_pending=True`` — the orchestrator has only one gate
            posted, so a bare "approved" is unambiguous.
      * If neither condition holds (non-threaded message + multiple gates
        pending), only the strict ``keywords`` regex set is consulted.
        That preserves the no-false-ACK guarantee of the original AB-03
        matcher.
      * When ``gate_post_ts`` is set the poll also fetches
        ``conversations.replies`` for that thread so threaded replies are
        considered (``conversations.history`` returns thread parents only).

    The strict ``keywords`` regex set always applies regardless of
    ``relaxed``; it is a superset of permissible matches, not an
    alternative.

    Returns:
      {"matched": True, "message_ts": "...", "text": "...",
       "matched_keyword": "...", "via": "thread"|"single_pending"|"strict"}
        on a match, or {"matched": False} if no matching message is found.
    """
    token = os.environ.get("SLACK_BOT_TOKEN_ALFRED") or os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return {"error": "SLACK_BOT_TOKEN_ALFRED not configured"}
    if not channel or not isinstance(channel, str):
        return {"error": "channel must be a non-empty string"}
    if not after_ts or not isinstance(after_ts, str):
        return {"error": "after_ts must be a non-empty string"}
    if not author_user_id or not isinstance(author_user_id, str):
        return {"error": "author_user_id must be a non-empty string"}
    if not isinstance(keywords, list) or not keywords:
        return {"error": "keywords must be a non-empty list of regex strings"}

    patterns: List[tuple[str, "re.Pattern[str]"]] = []
    for k in keywords:
        if not isinstance(k, str) or not k:
            return {"error": "each keyword must be a non-empty string"}
        try:
            patterns.append((k, re.compile(k, re.IGNORECASE)))
        except re.error as e:
            return {"error": f"invalid regex {k!r}: {e}"}

    relaxed_patterns: List[tuple[str, "re.Pattern[str]"]] = (
        _compile_relaxed_patterns() if relaxed else []
    )

    def _match_text(
        text: str,
        is_threaded_reply: bool,
    ) -> Optional[Dict[str, Any]]:
        """Apply strict + relaxed pattern sets per Fix E rules. Returns the
        match dict (without ``message_ts`` / ``text`` — caller fills those)
        or ``None`` if no rule fires.
        """
        # Strict patterns apply always — preserves the AB-03 guarantee.
        for raw_kw, pat in patterns:
            if pat.search(text):
                return {"matched_keyword": raw_kw, "via": "strict"}

        if not relaxed:
            return None

        # Relaxed gates: threaded reply OR single_pending. Without one of
        # these, a bare "approved" with multiple gates posted is too
        # ambiguous to accept.
        if is_threaded_reply:
            for raw_kw, pat in relaxed_patterns:
                if pat.search(text):
                    return {"matched_keyword": raw_kw, "via": "thread"}
        elif single_pending:
            for raw_kw, pat in relaxed_patterns:
                if pat.search(text):
                    return {
                        "matched_keyword": raw_kw,
                        "via": "single_pending",
                    }
        return None

    def _consider_message(
        msg: Dict[str, Any],
        *,
        force_threaded: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Apply author + match rules to one Slack message dict."""
        if msg.get("user") != author_user_id:
            return None
        text = msg.get("text") or ""
        # A message is "a threaded reply to the gate post" when its
        # ``thread_ts`` matches the gate's ``ts`` AND it isn't itself the
        # parent (parent has thread_ts == ts).
        msg_ts = msg.get("ts")
        msg_thread_ts = msg.get("thread_ts")
        is_threaded_reply = force_threaded or bool(
            gate_post_ts
            and msg_thread_ts == gate_post_ts
            and msg_ts != gate_post_ts
        )
        match = _match_text(text, is_threaded_reply=is_threaded_reply)
        if match is None:
            return None
        return {
            "matched": True,
            "message_ts": msg_ts,
            "text": text,
            **match,
        }

    def _build_url(base: str, query: str) -> str:
        return f"https://slack.com/api/{base}?{query}"

    async def _http_get(url: str) -> Dict[str, Any]:
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "saluca-alfred/1.0",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=SLACK_ACK_POLL_TIMEOUT_SEC) as r:
                if r.status != 200:
                    return {"_error": f"slack http {r.status}"}
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {
                "_error": f"slack http {e.code}: {e.read().decode(errors='replace')[:300]}"
            }
        except Exception as e:
            return {"_error": f"slack transport: {type(e).__name__}: {e}"}

    # ── Pass 1: scan threaded replies if a gate_post_ts was supplied. ──
    # `conversations.history` returns the thread parent only (no replies),
    # so threaded ACKs are invisible without a separate `conversations.replies`
    # call. We do this BEFORE the history scan because threaded ACKs are the
    # most common Cristian-friendly path and we want to short-circuit the
    # paginated history walk if we find one.
    if gate_post_ts:
        replies_qs = (
            f"channel={urllib.parse.quote(channel)}"
            f"&ts={urllib.parse.quote(gate_post_ts)}"
            f"&limit={SLACK_ACK_POLL_PAGE_LIMIT}"
        )
        replies_cursor: Optional[str] = None
        while True:
            qs = replies_qs
            if replies_cursor:
                qs += f"&cursor={urllib.parse.quote(replies_cursor)}"
            body = await _http_get(_build_url("conversations.replies", qs))
            if "_error" in body:
                # Surface the same error shape callers already handle. We
                # deliberately bail on the threaded scan rather than the
                # whole poll — falling through to history scan would
                # silently mask a transient API problem.
                return {"error": body["_error"]}
            if not body.get("ok"):
                return {
                    "error": f"slack {body.get('error', 'unknown')}",
                    "raw": body,
                }
            messages = body.get("messages") or []
            # `conversations.replies` returns the parent first then replies
            # in chronological order. Skip the parent (its `ts` equals the
            # gate post) and consider only the replies as threaded.
            for msg in messages:
                if msg.get("ts") == gate_post_ts:
                    continue
                hit = _consider_message(msg, force_threaded=True)
                if hit is not None:
                    return hit
            if not body.get("has_more"):
                break
            next_cursor = (
                (body.get("response_metadata") or {}).get("next_cursor")
            ) or ""
            if not next_cursor:
                break
            replies_cursor = next_cursor

    # ── Pass 2: scan channel history (existing behaviour). ──────────────
    cursor: Optional[str] = None
    while True:
        qs = (
            f"channel={urllib.parse.quote(channel)}"
            f"&oldest={urllib.parse.quote(after_ts)}"
            f"&limit={SLACK_ACK_POLL_PAGE_LIMIT}"
        )
        if cursor:
            qs += f"&cursor={urllib.parse.quote(cursor)}"
        body = await _http_get(_build_url("conversations.history", qs))
        if "_error" in body:
            return {"error": body["_error"]}
        if not body.get("ok"):
            return {"error": f"slack {body.get('error', 'unknown')}", "raw": body}

        messages = body.get("messages") or []
        # Slack returns messages newest-first; iterate oldest-first so the
        # "first match" is the earliest qualifying reply.
        for msg in reversed(messages):
            hit = _consider_message(msg)
            if hit is not None:
                return hit

        # Pagination. Slack surfaces `has_more` + `response_metadata.next_cursor`.
        if not body.get("has_more"):
            break
        next_cursor = ((body.get("response_metadata") or {}).get("next_cursor")) or ""
        if not next_cursor:
            break
        cursor = next_cursor

    return {"matched": False}


# ── linear_update_issue_state ───────────────────────────────────────────────

# Module-level cache: team_id -> {state_name_lower: state_id}. Linear team
# state IDs are stable; one lookup per team per process is plenty.
_LINEAR_TEAM_STATES_CACHE: Dict[str, Dict[str, str]] = {}


async def _linear_graphql(
    query: str,
    variables: Dict[str, Any],
    key: str,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """POST a GraphQL query to Linear. Returns (body, error-string)."""
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        LINEAR_GRAPHQL,
        data=payload,
        headers={
            "Authorization": key,
            "Content-Type": "application/json",
            "User-Agent": "saluca-alfred/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status != 200:
                return None, f"linear http {r.status}"
            return json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        return None, f"linear http {e.code}: {e.read().decode(errors='replace')[:300]}"
    except Exception as e:
        return None, f"linear transport: {type(e).__name__}: {e}"


# SAL-3038 / SAL-3070 (2026-04-28): the bare mesh-claim path in `main.py`
# was bypassing the human-assigned + terminal-state gate that PR #171
# added to the orchestrator path. When a Linear ticket gets the
# `human-assigned` label *after* mesh tasks for it have been queued
# (47 tasks queued for SAL-3038 at 00:40-00:54 UTC, label applied
# later), the bare-claim loop kept consuming them and dispatching
# builders, producing 22 zombie PRs. The orchestrator's existing check
# (orchestrator.py:3076-3084) only fires on hydrated `Ticket` objects
# inside the wave-dispatch loop — it never sees the bare mesh poll.
#
# This helper is the canonical "fetch labels + state for an identifier"
# entry point. Both paths call it. Returns `None` when the lookup is
# impossible (no key, transport error, ticket not found) so callers can
# fail-open (gate doesn't apply, dispatch proceeds — the orchestrator
# path will catch it on the next hydrate if Linear comes back).
async def linear_get_issue_status(
    ticket_code: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Fetch a Linear ticket's labels + workflow state by identifier.

    Args:
        ticket_code: Human Linear identifier (e.g. ``"SAL-3038"``). The
            GraphQL ``issue(id: $id)`` field accepts either UUID or the
            human identifier — see the note in
            ``_fetch_linear_acceptance_criteria``.

    Returns:
        ``{"identifier": str, "labels": list[str], "state": str}`` on
        success. ``None`` when:

          * ``ticket_code`` is empty / falsy,
          * neither ``LINEAR_API_KEY`` nor ``ALFRED_OPS_LINEAR_API_KEY``
            is set in the environment,
          * the Linear GraphQL request fails (transport / non-200),
          * the issue is not found.

        Callers MUST treat ``None`` as fail-open (proceed with
        dispatch). The cost of an extra builder run is bounded
        (~6 min), but blocking dispatch on Linear flakiness would stall
        the entire mesh poll loop.

    Cost: one Linear GraphQL round-trip per call. The bare claim path
    polls every ~50s with at most ``limit=10`` tasks per tick, so even
    the worst case is ~12 calls/min — well under Linear's 1500/hour
    quota. No caching here; the whole point of this gate is to catch
    label changes that landed *after* mesh tasks were queued.
    """
    if not ticket_code:
        return None
    key = os.environ.get("LINEAR_API_KEY") or os.environ.get(
        "ALFRED_OPS_LINEAR_API_KEY"
    )
    if not key:
        return None

    query = (
        "query IssueStatus($id: String!) { "
        "issue(id: $id) { "
        "identifier "
        "labels { nodes { name } } "
        "state { name } "
        "} }"
    )
    body, err = await _linear_graphql(query, {"id": ticket_code}, key)
    if err is not None or body is None:
        return None
    issue = (body.get("data") or {}).get("issue") or None
    if not isinstance(issue, dict) or not issue:
        return None
    labels_block = issue.get("labels") or {}
    label_nodes = labels_block.get("nodes") if isinstance(labels_block, dict) else None
    labels = [
        n.get("name")
        for n in (label_nodes or [])
        if isinstance(n, dict) and isinstance(n.get("name"), str)
    ]
    state_block = issue.get("state") or {}
    state_name = state_block.get("name") if isinstance(state_block, dict) else None
    return {
        "identifier": issue.get("identifier") or ticket_code,
        "labels": labels,
        "state": state_name or "",
    }


async def _linear_load_team_states(team_id: str, key: str) -> tuple[Dict[str, str], Optional[str]]:
    """Fetch + cache all workflow states for a Linear team. Returns (map, err)."""
    cached = _LINEAR_TEAM_STATES_CACHE.get(team_id)
    if cached is not None:
        return cached, None

    query = (
        "query TeamStates($teamId: String!) { "
        "team(id: $teamId) { id name states { nodes { id name type } } } }"
    )
    body, err = await _linear_graphql(query, {"teamId": team_id}, key)
    if err is not None:
        return {}, err
    data = (body or {}).get("data") or {}
    team = data.get("team") or {}
    nodes = ((team.get("states") or {}).get("nodes")) or []
    if not nodes:
        return {}, f"linear team {team_id!r} has no workflow states"
    state_map: Dict[str, str] = {}
    for n in nodes:
        name = n.get("name")
        sid = n.get("id")
        if name and sid:
            state_map[name.lower()] = sid
    _LINEAR_TEAM_STATES_CACHE[team_id] = state_map
    return state_map, None


async def linear_update_issue_state(
    issue_id: str,
    state_name: str,
) -> Dict[str, Any]:
    """Transition a Linear issue to a named workflow state (scoped to its team).

    Looks up the issue's team, resolves `state_name` against that team's
    workflow states (NOT global — Linear state IDs are per-team), and issues
    the `issueUpdate` mutation.

    `issue_id` may be either the UUID or the human identifier (e.g. "SAL-2680").
    Returns `{ok, identifier, state}` on success, or `{error, ...}` on failure.
    """
    key = os.environ.get("LINEAR_API_KEY") or os.environ.get("ALFRED_OPS_LINEAR_API_KEY")
    if not key:
        return {"error": "LINEAR_API_KEY not configured"}
    if not issue_id or not isinstance(issue_id, str):
        return {"error": "issue_id must be a non-empty string"}
    if not state_name or not isinstance(state_name, str):
        return {"error": "state_name must be a non-empty string"}

    # 1. Resolve issue -> {id, team.id, identifier}. The `issue(id:)` query
    # accepts either the UUID or the human identifier directly.
    issue_query = (
        "query IssueLookup($id: String!) { "
        "issue(id: $id) { id identifier team { id } state { name } } }"
    )
    body, err = await _linear_graphql(issue_query, {"id": issue_id}, key)
    if err is not None:
        return {"error": err}
    issue = ((body or {}).get("data") or {}).get("issue") or {}
    if not issue.get("id"):
        return {"error": f"linear issue {issue_id!r} not found"}
    uuid = issue["id"]
    identifier = issue.get("identifier")
    team_id = (issue.get("team") or {}).get("id")
    if not team_id:
        return {"error": "linear issue missing team id"}

    # 2. Resolve state name -> state id (cached per team).
    state_map, err = await _linear_load_team_states(team_id, key)
    if err is not None:
        return {"error": err}
    state_id = state_map.get(state_name.lower())
    if not state_id:
        available = sorted(state_map.keys())
        return {
            "error": f"state {state_name!r} not found on team {team_id}",
            "available_states": available,
        }

    # 3. Issue the mutation.
    mutation = (
        "mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) { "
        "issueUpdate(id: $id, input: $input) "
        "{ success issue { identifier state { name } } } }"
    )
    body, err = await _linear_graphql(
        mutation,
        {"id": uuid, "input": {"stateId": state_id}},
        key,
    )
    if err is not None:
        return {"error": err}
    result = ((body or {}).get("data") or {}).get("issueUpdate") or {}
    if not result.get("success"):
        return {"error": "linear issueUpdate returned success=false", "raw": body}
    out_issue = result.get("issue") or {}
    return {
        "ok": True,
        "identifier": out_issue.get("identifier") or identifier,
        "state": (out_issue.get("state") or {}).get("name") or state_name,
    }


# ── linear_add_label_to_issue ──────────────────────────────────────────────

# Module-level cache: team_id -> {label_name_lower: label_id}. Linear team
# label IDs are stable; one lookup per team per process is plenty. Mirrors
# ``_LINEAR_TEAM_STATES_CACHE`` shape and lifetime so a cold daemon does
# one extra GraphQL hit per team and never thinks about it again.
_LINEAR_TEAM_LABELS_CACHE: Dict[str, Dict[str, str]] = {}


async def _linear_load_team_labels(
    team_id: str, key: str,
) -> tuple[Dict[str, str], Optional[str]]:
    """Fetch + cache all labels for a Linear team. Returns (map, err).

    Pages through ``team.labels(after: $cursor)`` because a team can have
    50+ labels (SAL has the wave-N / size-S/M/L / epic / status families).
    """
    cached = _LINEAR_TEAM_LABELS_CACHE.get(team_id)
    if cached is not None:
        return cached, None

    state_map: Dict[str, str] = {}
    cursor: Optional[str] = None
    # Paged fetch. 100 labels/page is well under Linear's complexity cap
    # for this lightweight payload (id + name only).
    while True:
        query = (
            "query TeamLabels($teamId: String!, $after: String) { "
            "team(id: $teamId) { id labels(first: 100, after: $after) "
            "{ nodes { id name } pageInfo { hasNextPage endCursor } } } }"
        )
        variables: Dict[str, Any] = {"teamId": team_id}
        if cursor:
            variables["after"] = cursor
        body, err = await _linear_graphql(query, variables, key)
        if err is not None:
            return {}, err
        data = (body or {}).get("data") or {}
        team = data.get("team") or {}
        labels = (team.get("labels") or {})
        nodes = labels.get("nodes") or []
        for n in nodes:
            name = n.get("name")
            lid = n.get("id")
            if name and lid:
                state_map[name.lower()] = lid
        page_info = labels.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break
    _LINEAR_TEAM_LABELS_CACHE[team_id] = state_map
    return state_map, None


async def linear_add_label_to_issue(
    issue_id: str,
    label_name: str,
) -> Dict[str, Any]:
    """Attach a named label to a Linear issue (scoped to its team).

    Mirrors ``linear_update_issue_state``: looks up the issue's team,
    resolves ``label_name`` against that team's labels (NOT global —
    Linear label IDs are per-team), then issues ``issueAddLabel``.

    ``issue_id`` may be either the UUID or the human identifier (e.g.
    ``"SAL-2680"``). Idempotent on the Linear side: re-adding a label
    already on the issue is a no-op success per Linear's API contract,
    so the orchestrator does not need to pre-check.

    Returns ``{ok, identifier, label}`` on success, ``{error, ...}`` on
    failure. Failure is non-fatal at the orchestrator layer — the helper
    that calls this swallows errors after logging, same as
    ``_update_linear_state``.
    """
    key = os.environ.get("LINEAR_API_KEY") or os.environ.get(
        "ALFRED_OPS_LINEAR_API_KEY"
    )
    if not key:
        return {"error": "LINEAR_API_KEY not configured"}
    if not issue_id or not isinstance(issue_id, str):
        return {"error": "issue_id must be a non-empty string"}
    if not label_name or not isinstance(label_name, str):
        return {"error": "label_name must be a non-empty string"}

    # 1. Resolve issue -> {id, team.id, identifier}.
    issue_query = (
        "query IssueLookup($id: String!) { "
        "issue(id: $id) { id identifier team { id } } }"
    )
    body, err = await _linear_graphql(issue_query, {"id": issue_id}, key)
    if err is not None:
        return {"error": err}
    issue = ((body or {}).get("data") or {}).get("issue") or {}
    if not issue.get("id"):
        return {"error": f"linear issue {issue_id!r} not found"}
    uuid = issue["id"]
    identifier = issue.get("identifier")
    team_id = (issue.get("team") or {}).get("id")
    if not team_id:
        return {"error": "linear issue missing team id"}

    # 2. Resolve label name -> label id (cached per team).
    label_map, err = await _linear_load_team_labels(team_id, key)
    if err is not None:
        return {"error": err}
    label_id = label_map.get(label_name.lower())
    if not label_id:
        available = sorted(label_map.keys())
        return {
            "error": f"label {label_name!r} not found on team {team_id}",
            "available_labels": available,
        }

    # 3. Issue the mutation. ``issueAddLabel`` is idempotent server-side.
    mutation = (
        "mutation IssueAddLabel($id: String!, $labelId: String!) { "
        "issueAddLabel(id: $id, labelId: $labelId) "
        "{ success issue { identifier labels { nodes { name } } } } }"
    )
    body, err = await _linear_graphql(
        mutation,
        {"id": uuid, "labelId": label_id},
        key,
    )
    if err is not None:
        return {"error": err}
    result = ((body or {}).get("data") or {}).get("issueAddLabel") or {}
    if not result.get("success"):
        return {"error": "linear issueAddLabel returned success=false", "raw": body}
    out_issue = result.get("issue") or {}
    return {
        "ok": True,
        "identifier": out_issue.get("identifier") or identifier,
        "label": label_name,
    }


# ── linear_add_comment ─────────────────────────────────────────────────────

# Module-level cache: identifier (e.g. "SAL-2680") -> issue UUID. Linear
# issue UUIDs are stable; one lookup per identifier per process is plenty.
# The orchestrator's audit/escalation comment paths frequently target the
# same parent ticket multiple times in a single tick (destructive-guardrail
# trip + ticket_escalated event + later stale-sweep), so caching avoids
# N redundant GraphQL hits. Mirrors ``_LINEAR_TEAM_STATES_CACHE`` /
# ``_LINEAR_TEAM_LABELS_CACHE`` shape and lifetime.
_LINEAR_ISSUE_UUID_CACHE: Dict[str, str] = {}


async def linear_add_comment(
    issue_id: str,
    body: str,
) -> Dict[str, Any]:
    """Post a markdown comment on a Linear issue.

    Mirrors ``linear_update_issue_state`` / ``linear_add_label_to_issue``:
    ``issue_id`` accepts either the UUID or the human identifier (e.g.
    ``"SAL-2680"``); identifier->UUID resolution is cached so repeat
    comments on the same parent in one tick collapse to a single lookup.

    Issues the GraphQL ``commentCreate`` mutation. Returns
    ``{ok, comment_id, url, identifier}`` on success, ``{error, ...}`` on
    failure. Failure is non-fatal at the orchestrator layer — the helpers
    that call this swallow errors after logging, same as
    ``_post_destructive_guardrail_linear_comment`` and the stale-sweep
    audit path.
    """
    key = os.environ.get("LINEAR_API_KEY") or os.environ.get(
        "ALFRED_OPS_LINEAR_API_KEY"
    )
    if not key:
        return {"error": "LINEAR_API_KEY not configured"}
    if not issue_id or not isinstance(issue_id, str):
        return {"error": "issue_id must be a non-empty string"}
    if not body or not isinstance(body, str):
        return {"error": "body must be a non-empty string"}

    # 1. Resolve issue -> UUID (cached). The ``issue(id:)`` query accepts
    # either a UUID or a human identifier directly, so a UUID input round-
    # trips through here cheaply on cache miss.
    cached_uuid = _LINEAR_ISSUE_UUID_CACHE.get(issue_id)
    identifier: Optional[str] = None
    if cached_uuid is not None:
        uuid = cached_uuid
    else:
        issue_query = (
            "query IssueLookup($id: String!) { "
            "issue(id: $id) { id identifier } }"
        )
        body_resp, err = await _linear_graphql(issue_query, {"id": issue_id}, key)
        if err is not None:
            return {"error": err}
        issue = ((body_resp or {}).get("data") or {}).get("issue") or {}
        if not issue.get("id"):
            return {"error": f"linear issue {issue_id!r} not found"}
        uuid = issue["id"]
        identifier = issue.get("identifier")
        _LINEAR_ISSUE_UUID_CACHE[issue_id] = uuid
        # Also cache by identifier so a subsequent call with the human
        # form short-circuits even if this call passed the UUID.
        if identifier and identifier != issue_id:
            _LINEAR_ISSUE_UUID_CACHE[identifier] = uuid

    # 2. Issue the mutation.
    mutation = (
        "mutation CommentCreate($input: CommentCreateInput!) { "
        "commentCreate(input: $input) "
        "{ success comment { id url } } }"
    )
    body_resp, err = await _linear_graphql(
        mutation,
        {"input": {"issueId": uuid, "body": body}},
        key,
    )
    if err is not None:
        return {"error": err}
    result = ((body_resp or {}).get("data") or {}).get("commentCreate") or {}
    if not result.get("success"):
        return {"error": "linear commentCreate returned success=false", "raw": body_resp}
    comment = result.get("comment") or {}
    return {
        "ok": True,
        "comment_id": comment.get("id"),
        "url": comment.get("url"),
        "identifier": identifier or issue_id,
    }


# ── linear_list_project_issues ──────────────────────────────────────────────

LINEAR_LIST_PAGE_SIZE = 25  # Linear complexity cap ~10000 hit at page=100 with labels+state+relations (observed 12081 on SAL project, 2026-04-23)
LINEAR_LIST_DEFAULT_LIMIT = 250


async def linear_list_project_issues(
    project_id: str,
    limit: int = LINEAR_LIST_DEFAULT_LIMIT,
) -> Dict[str, Any]:
    """List all issues in a Linear project with the fields the orchestrator needs.

    Paginates `issues(first: 100, after: $cursor)` until the project is fully
    drained or the `limit` cap is hit. Returns a top-level dict with the
    `issues` list so callers can also see `total` + `truncated` at a glance.

    Per-issue shape:
      {id, identifier, title, labels[], estimate,
       state: {name},
       relations: [{type, relatedIssue: {id, identifier}}]}
    """
    key = os.environ.get("LINEAR_API_KEY") or os.environ.get("ALFRED_OPS_LINEAR_API_KEY")
    if not key:
        return {"error": "LINEAR_API_KEY not configured"}
    if not project_id or not isinstance(project_id, str):
        return {"error": "project_id must be a non-empty string"}
    try:
        limit_int = int(limit)
    except (TypeError, ValueError):
        return {"error": "limit must be an integer"}
    if limit_int <= 0:
        return {"error": "limit must be positive"}

    # dynamic-hints-from-ticket-body refactor (2026-04-29): added
    # ``description`` so the orchestrator's hint resolver can parse the
    # embedded ``## Target`` block at dispatch time without a per-ticket
    # follow-up query. Linear's complexity budget is roomy enough at
    # page_size=25 (the existing cap); description is a single string
    # field per issue.
    query = (
        "query ProjectIssues($projectId: String!, $first: Int!, $after: String) { "
        "project(id: $projectId) { "
        "id name "
        "issues(first: $first, after: $after) { "
        "pageInfo { hasNextPage endCursor } "
        "nodes { "
        "id identifier title description estimate "
        "labels { nodes { name } } "
        "state { name } "
        "relations { nodes { type relatedIssue { id identifier } } } "
        "} } } }"
    )

    out: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    truncated = False
    while True:
        page_size = min(LINEAR_LIST_PAGE_SIZE, limit_int - len(out))
        if page_size <= 0:
            truncated = True
            break
        variables: Dict[str, Any] = {
            "projectId": project_id,
            "first": page_size,
        }
        if cursor:
            variables["after"] = cursor
        body, err = await _linear_graphql(query, variables, key)
        if err is not None:
            return {"error": err}
        project = ((body or {}).get("data") or {}).get("project")
        if not project:
            return {"error": f"linear project {project_id!r} not found"}
        issues = (project.get("issues") or {})
        nodes = issues.get("nodes") or []
        for n in nodes:
            labels = [(lbl.get("name") or "") for lbl in ((n.get("labels") or {}).get("nodes") or [])]
            relations = [
                {
                    "type": r.get("type"),
                    "relatedIssue": {
                        "id": ((r.get("relatedIssue") or {}).get("id")),
                        "identifier": ((r.get("relatedIssue") or {}).get("identifier")),
                    },
                }
                for r in ((n.get("relations") or {}).get("nodes") or [])
            ]
            out.append({
                "id": n.get("id"),
                "identifier": n.get("identifier"),
                "title": n.get("title"),
                "description": n.get("description") or "",
                "labels": labels,
                "estimate": n.get("estimate"),
                "state": {"name": ((n.get("state") or {}).get("name"))},
                "relations": relations,
            })
            if len(out) >= limit_int:
                break
        page_info = issues.get("pageInfo") or {}
        if len(out) >= limit_int:
            truncated = bool(page_info.get("hasNextPage"))
            break
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break

    return {
        "issues": out,
        "total": len(out),
        "truncated": truncated,
    }


# ── linear_get_issue_relations ──────────────────────────────────────────────

# Linear relation types the orchestrator cares about. `blocks` / `blocked_by`
# are the two sides of the same edge; `related` covers the soft link.
_RELATION_TYPE_BUCKETS = {
    "blocks": "blocks",
    "blocked_by": "blocked_by",
    "related": "related",
    "duplicate": "related",
    "duplicate_of": "related",
}


async def linear_get_issue_relations(issue_id: str) -> Dict[str, Any]:
    """Fetch the relations for one Linear issue, bucketed by direction.

    Returns `{blocks: [...], blocked_by: [...], related: [...]}` — each list
    holds the identifiers (e.g. "SAL-2680") of the OTHER side of the edge.
    Unknown relation types land in `related` so the orchestrator never drops
    dependency info silently.
    """
    key = os.environ.get("LINEAR_API_KEY") or os.environ.get("ALFRED_OPS_LINEAR_API_KEY")
    if not key:
        return {"error": "LINEAR_API_KEY not configured"}
    if not issue_id or not isinstance(issue_id, str):
        return {"error": "issue_id must be a non-empty string"}

    query = (
        "query IssueRelations($id: String!) { "
        "issue(id: $id) { "
        "id identifier "
        "relations { nodes { type relatedIssue { id identifier state { name } } } } "
        "} }"
    )
    body, err = await _linear_graphql(query, {"id": issue_id}, key)
    if err is not None:
        return {"error": err}
    issue = ((body or {}).get("data") or {}).get("issue") or {}
    if not issue.get("id"):
        return {"error": f"linear issue {issue_id!r} not found"}

    buckets: Dict[str, List[str]] = {"blocks": [], "blocked_by": [], "related": []}
    for r in ((issue.get("relations") or {}).get("nodes") or []):
        rtype = (r.get("type") or "").lower()
        related = r.get("relatedIssue") or {}
        ident = related.get("identifier")
        if not ident:
            continue
        bucket = _RELATION_TYPE_BUCKETS.get(rtype, "related")
        buckets[bucket].append(ident)

    return {
        "identifier": issue.get("identifier"),
        "blocks": buckets["blocks"],
        "blocked_by": buckets["blocked_by"],
        "related": buckets["related"],
    }


# ── Registry ────────────────────────────────────────────────────────────────

BUILTIN_TOOLS: Dict[str, ToolSpec] = {
    "linear_create_issue": ToolSpec(
        name="linear_create_issue",
        description=(
            "Create a Linear issue in the Saluca SAL team. Use for follow-up work, "
            "bug reports, or feature requests that should land on the team backlog. "
            "Priority: 1=urgent, 2=high, 3=medium, 4=low (0=no priority, default 3)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short imperative title"},
                "description": {
                    "type": "string",
                    "description": "Markdown body. Include context, links, and acceptance criteria.",
                },
                "priority": {
                    "type": "integer",
                    "description": "0-4. Default 3 (medium).",
                    "minimum": 0,
                    "maximum": 4,
                },
                "due_date": {
                    "type": "string",
                    "description": "Optional due date in YYYY-MM-DD format.",
                },
            },
            "required": ["title"],
            "additionalProperties": False,
        },
        handler=linear_create_issue,
    ),
    "slack_post": ToolSpec(
        name="slack_post",
        description=(
            "Post a short status message to Slack. Defaults to the #batcave COO "
            "status channel unless a specific channel id is passed. Use sparingly: "
            "status updates, escalations, questions for Cristian."
        ),
        parameters={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Message body (markdown acceptable)"},
                "channel": {
                    "type": "string",
                    "description": "Optional Slack channel id. Defaults to batcave.",
                },
            },
            "required": ["message"],
            "additionalProperties": False,
        },
        handler=slack_post,
    ),
    "mesh_task_create": ToolSpec(
        name="mesh_task_create",
        description=(
            "Create a new mesh task that any daemon persona can claim. Use to "
            "fan out work to specialist personas (e.g. delegate a PQ/crypto "
            "review to riddler-crypto-a, a revenue question to maxwell-lord-a, "
            "or a PR-level QA sweep to hawkman-qa-a). The `persona` "
            "argument routes the task; tags are optional free-form labels."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short task title"},
                "description": {"type": "string", "description": "Full task description + context"},
                "persona": {
                    "type": "string",
                    "description": "Persona to route the task to (e.g. 'riddler-crypto-a', 'maxwell-lord-a', 'hawkman-qa-a'). Optional.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional bracketed tags to prepend to the title (e.g. ['unified-plan-wave-2']).",
                },
            },
            "required": ["title"],
            "additionalProperties": False,
        },
        handler=mesh_task_create,
    ),
    "http_get": ToolSpec(
        name="http_get",
        description=(
            "GET an allowlisted URL. Read-only; no POST/PUT/DELETE. Useful for "
            "pulling file content from Saluca GitHub repos (github.com/salucallc, "
            "saluca-labs, cristianxruvalcaba-coder/...), raw.githubusercontent.com "
            "files, Saluca domains (*.saluca.com, *.tiresias.network, *.asphodel.ai), "
            "arxiv papers, and canonical docs (anthropic, python, github). "
            "Response body is capped at 256 KB and truncated with a marker if "
            "larger; only text/json/xml/yaml content types are returned."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Absolute http(s) URL. Must be in the allowlist.",
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        handler=http_get,
    ),
    "propose_pr": ToolSpec(
        name="propose_pr",
        description=(
            "Atomic repo modification: clone the target repo, create a branch, "
            "write the given files, commit + push, and open a pull request. "
            "Returns the PR URL on success. Only Saluca-owned repos are allowed "
            "(salucallc, saluca-labs, cristianxruvalcaba-coder). File paths must "
            "be relative. This is the primary tool for autonomous code changes. "
            "REQUIRED: the `body` argument MUST contain a "
            "`## APE/V Acceptance (machine-checkable)` heading followed by the "
            "byte-verbatim acceptance lines from the Linear ticket body, or "
            "hawkman QA will REQUEST_CHANGES with reason 'missing APE/V "
            "citation' (75% of v7af rejects). Do not paraphrase the acceptance "
            "text — copy it exactly from the dispatched task body's "
            "`## APE/V Acceptance (machine-checkable)` section, or from the "
            "Linear ticket if the task body did not pre-render it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "GitHub org/user. Must be salucallc, saluca-labs, or cristianxruvalcaba-coder.",
                },
                "repo": {"type": "string", "description": "Repo name"},
                "branch": {"type": "string", "description": "Feature branch to create (e.g. 'feature/my-change')"},
                "base_branch": {
                    "type": "string",
                    "description": "Base branch, typically 'main'.",
                },
                "title": {"type": "string", "description": "PR title"},
                "body": {
                    "type": "string",
                    "description": (
                        "PR body (markdown). MUST contain a "
                        "`## APE/V Acceptance (machine-checkable)` heading "
                        "with the byte-verbatim acceptance lines from the "
                        "Linear ticket body — no paraphrasing, no "
                        "reformatting. Hawkman QA REQUEST_CHANGES on any "
                        "PR whose body lacks this heading, and the helper "
                        "auto-inject only fires when the heading is ABSENT, "
                        "so a paraphrased citation reaches hawkman and "
                        "fails GATE 1's verbatim substring match. Best "
                        "practice: paste the canonical block as the FIRST "
                        "section of the body, then your normal "
                        "Summary/Diff/Tests sections."
                    ),
                },
                "files": {
                    "type": "object",
                    "description": "Mapping of relative-path -> file-content. Each file is written, added, committed, and pushed.",
                    "additionalProperties": {"type": "string"},
                },
                "commit_message": {
                    "type": "string",
                    "description": "Commit message (defaults to the PR title if omitted).",
                },
            },
            "required": ["owner", "repo", "branch", "title", "body", "files"],
            "additionalProperties": False,
        },
        handler=propose_pr,
    ),
    "update_pr": ToolSpec(
        name="update_pr",
        description=(
            "Push file updates to an EXISTING pull request's feature branch. "
            "AB-17-o fix-round companion to propose_pr: when a task body "
            "contains a `## Prior PR` section (set by the orchestrator on a "
            "REQUEST_CHANGES respawn), call update_pr with that PR URL + "
            "branch instead of propose_pr so the original review thread is "
            "preserved and no duplicate PR is opened. Refuses closed / "
            "merged PRs and missing branches (that's propose_pr's job). "
            "Only Saluca-owned repos (salucallc, saluca-labs, "
            "cristianxruvalcaba-coder) are allowed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pr_url": {
                    "type": "string",
                    "description": "Full PR URL (https://github.com/<owner>/<repo>/pull/<n>).",
                },
                "branch": {
                    "type": "string",
                    "description": "Existing feature branch on the PR head (e.g. 'feature/sal-2615-x').",
                },
                "commit_message": {
                    "type": "string",
                    "description": "Commit message for the fix-round push. Required.",
                },
                "files": {
                    "type": "array",
                    "description": "Files to overwrite. Each item: {path, content}.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                        "additionalProperties": False,
                    },
                },
                "title": {
                    "type": "string",
                    "description": "Optional PR title replacement. Omit to keep existing.",
                },
                "body": {
                    "type": "string",
                    "description": (
                        "Optional PR body replacement. Omit to keep "
                        "existing. If you DO replace the body, it MUST "
                        "still contain a `## APE/V Acceptance "
                        "(machine-checkable)` heading with byte-verbatim "
                        "acceptance lines from the Linear ticket — "
                        "fix-round respawns must not regress GATE 1. The "
                        "auto-inject is skipped on update_pr (preserves "
                        "the original PR body's citation), so YOU own the "
                        "verbatim block on every update_pr that passes "
                        "body."
                    ),
                },
                "force_push": {
                    "type": "boolean",
                    "description": "Use --force-with-lease on the push. Default false.",
                },
            },
            "required": ["pr_url", "branch", "commit_message", "files"],
            "additionalProperties": False,
        },
        handler=update_pr,
    ),
    "pr_review": ToolSpec(
        name="pr_review",
        description=(
            "Submit a pull-request review on a Saluca-owned repo. Posts "
            "to the GitHub reviews endpoint with an event of APPROVE, "
            "REQUEST_CHANGES, or COMMENT, an overall body, and optional "
            "inline line comments. Only Saluca-owned orgs are allowed "
            "(salucallc, saluca-labs, cristianxruvalcaba-coder). Use this "
            "for QA/security verifier personas that review code they did "
            "not build."
        ),
        parameters={
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "GitHub org/user. Must be salucallc, saluca-labs, or cristianxruvalcaba-coder.",
                },
                "repo": {"type": "string", "description": "Repo name"},
                "pr_number": {
                    "type": "integer",
                    "description": "Pull request number (positive integer).",
                },
                "event": {
                    "type": "string",
                    "description": "Review verdict.",
                    "enum": ["APPROVE", "REQUEST_CHANGES", "COMMENT"],
                },
                "body": {
                    "type": "string",
                    "description": "Overall review body (markdown).",
                },
                "line_comments": {
                    "type": "array",
                    "description": "Optional inline comments. Each item: {path, line, body}.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "line": {"type": "integer"},
                            "body": {"type": "string"},
                        },
                        "required": ["path", "line", "body"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["owner", "repo", "pr_number", "event", "body"],
            "additionalProperties": False,
        },
        handler=pr_review,
    ),
    "pr_files_get": ToolSpec(
        name="pr_files_get",
        description=(
            "Fetch all files in a pull request with content at head SHA. "
            "Authenticated — works on private repos in allowlisted orgs. "
            "Use this in QA/review workflows to read the change surface in "
            "a single tool call."
        ),
        parameters={
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "GitHub org/user. Must be salucallc, saluca-labs, or cristianxruvalcaba-coder.",
                },
                "repo": {"type": "string", "description": "Repo name"},
                "pr_number": {
                    "type": "integer",
                    "description": "Pull request number (positive integer).",
                },
            },
            "required": ["owner", "repo", "pr_number"],
            "additionalProperties": False,
        },
        handler=pr_files_get,
    ),
    "github_merge_pr": ToolSpec(
        name="github_merge_pr",
        description=(
            "Merge a pull request on a Saluca-owned repo. Posts PUT to the "
            "GitHub merge endpoint with merge_method (squash, merge, or "
            "rebase) and optional commit_title/commit_message. Only Saluca-"
            "owned orgs are allowed (salucallc, saluca-labs, cristianxruvalcaba-"
            "coder). Used by the autonomous_build orchestrator after a QA "
            "persona has APPROVE'd the PR. Structured errors on 405 "
            "(not_mergeable), 409 (stale_head), 422 (unprocessable)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "GitHub org/user. Must be salucallc, saluca-labs, or cristianxruvalcaba-coder.",
                },
                "repo": {"type": "string", "description": "Repo name"},
                "pr_number": {
                    "type": "integer",
                    "description": "Pull request number (positive integer).",
                },
                "merge_method": {
                    "type": "string",
                    "description": "GitHub merge strategy. Default squash.",
                    "enum": ["squash", "merge", "rebase"],
                },
                "commit_title": {
                    "type": "string",
                    "description": "Optional override for the merge commit title.",
                },
                "commit_message": {
                    "type": "string",
                    "description": "Optional override for the merge commit body.",
                },
            },
            "required": ["owner", "repo", "pr_number"],
            "additionalProperties": False,
        },
        handler=github_merge_pr,
    ),
    "slack_ack_poll": ToolSpec(
        name="slack_ack_poll",
        description=(
            "Poll a Slack channel for the first message from a specific author "
            "(after a given timestamp) whose text matches any of the supplied "
            "regex keywords (case-insensitive). Paginates via cursor. Used by "
            "the autonomous_build orchestrator's SS-08 gate to wait on a "
            "Cristian ACK before dispatching sensitive tickets. Requires the "
            "bot to have `channels:history` scope on the target channel. "
            "Optionally accepts a relaxed-matching mode that recognises "
            "shortened ACK tokens (`approved`, `lgtm`, `+1`, 👍, ✅) when "
            "either (a) the message is a threaded reply to the gate post or "
            "(b) only one gate is currently pending."
        ),
        parameters={
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "Slack channel id (e.g. 'C0ASAKFTR1C' for #batcave).",
                },
                "after_ts": {
                    "type": "string",
                    "description": "Only consider messages posted after this Slack ts (unix float as string).",
                },
                "author_user_id": {
                    "type": "string",
                    "description": "Slack user id of the approver (resolved via users.lookupByEmail).",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Regex patterns; matched case-insensitive against message text.",
                },
                "gate_post_ts": {
                    "type": "string",
                    "description": (
                        "Optional Slack ts of the gate post itself. When "
                        "provided AND `relaxed=true`, threaded replies "
                        "(`thread_ts == gate_post_ts`) are matched against "
                        "the relaxed token set in addition to the strict "
                        "`keywords`."
                    ),
                },
                "relaxed": {
                    "type": "boolean",
                    "description": (
                        "Opt into the shortened-token set "
                        "(`approved`/`lgtm`/`+1`/👍/✅). The strict regex "
                        "still applies; the relaxed set is additive and only "
                        "fires under thread or single-gate-pending guards."
                    ),
                },
                "single_pending": {
                    "type": "boolean",
                    "description": (
                        "Caller asserts that only one gate is currently "
                        "posted and waiting for ACK. With `relaxed=true`, "
                        "non-threaded short-form replies are accepted under "
                        "this guard alone."
                    ),
                },
            },
            "required": ["channel", "after_ts", "author_user_id", "keywords"],
            "additionalProperties": False,
        },
        handler=slack_ack_poll,
    ),
    "linear_update_issue_state": ToolSpec(
        name="linear_update_issue_state",
        description=(
            "Transition a Linear issue to a named workflow state (scoped to the "
            "issue's team). Looks up the issue's team, resolves the state name "
            "against that team's states (Backlog / Todo / In Progress / In "
            "Review / Done / Canceled / Duplicate — whatever the team has), "
            "then issues the `issueUpdate` mutation. `issue_id` accepts either "
            "the UUID or the human identifier (e.g. 'SAL-2680')."
        ),
        parameters={
            "type": "object",
            "properties": {
                "issue_id": {
                    "type": "string",
                    "description": "Linear issue UUID or identifier (e.g. 'SAL-2680').",
                },
                "state_name": {
                    "type": "string",
                    "description": "Target workflow state name (case-insensitive).",
                },
            },
            "required": ["issue_id", "state_name"],
            "additionalProperties": False,
        },
        handler=linear_update_issue_state,
    ),
    "linear_add_label_to_issue": ToolSpec(
        name="linear_add_label_to_issue",
        description=(
            "Attach a named label to a Linear issue (scoped to the issue's "
            "team). Looks up the issue's team, resolves the label name "
            "against that team's labels, then issues the `issueAddLabel` "
            "mutation. Idempotent server-side: re-adding an existing label "
            "is a no-op success. `issue_id` accepts either the UUID or the "
            "human identifier (e.g. 'SAL-2680')."
        ),
        parameters={
            "type": "object",
            "properties": {
                "issue_id": {
                    "type": "string",
                    "description": "Linear issue UUID or identifier (e.g. 'SAL-2680').",
                },
                "label_name": {
                    "type": "string",
                    "description": "Target label name (case-insensitive).",
                },
            },
            "required": ["issue_id", "label_name"],
            "additionalProperties": False,
        },
        handler=linear_add_label_to_issue,
    ),
    "linear_add_comment": ToolSpec(
        name="linear_add_comment",
        description=(
            "Post a markdown comment on a Linear issue. Resolves "
            "`issue_id` (UUID or human identifier like 'SAL-2680') to "
            "the underlying issue UUID and issues the `commentCreate` "
            "mutation. Identifier->UUID lookups cache per-process so "
            "repeat comments on the same parent within one tick collapse "
            "to a single GraphQL hit. Used by the autonomous_build "
            "orchestrator's audit / escalation / destructive-guardrail / "
            "stale-sweep paths."
        ),
        parameters={
            "type": "object",
            "properties": {
                "issue_id": {
                    "type": "string",
                    "description": "Linear issue UUID or identifier (e.g. 'SAL-2680').",
                },
                "body": {
                    "type": "string",
                    "description": "Comment body (markdown supported).",
                },
            },
            "required": ["issue_id", "body"],
            "additionalProperties": False,
        },
        handler=linear_add_comment,
    ),
    "linear_list_project_issues": ToolSpec(
        name="linear_list_project_issues",
        description=(
            "List all issues in a Linear project. Paginates `issues(first: 100)` "
            "until drained or `limit` is reached. Returns each issue with "
            "{id, identifier, title, labels, estimate, state, relations}. "
            "Used by the autonomous_build orchestrator to build the wave + "
            "dependency graph up-front."
        ),
        parameters={
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Linear project UUID.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of issues to return (default 250).",
                    "minimum": 1,
                },
            },
            "required": ["project_id"],
            "additionalProperties": False,
        },
        handler=linear_list_project_issues,
    ),
    "linear_get_issue_relations": ToolSpec(
        name="linear_get_issue_relations",
        description=(
            "Fetch the relations for one Linear issue, bucketed by direction. "
            "Returns {identifier, blocks: [...], blocked_by: [...], related: [...]} "
            "where each list holds identifiers of the OTHER side of the edge. "
            "Unknown relation types fall into `related` so dependency info is "
            "never dropped silently."
        ),
        parameters={
            "type": "object",
            "properties": {
                "issue_id": {
                    "type": "string",
                    "description": "Linear issue UUID or identifier (e.g. 'SAL-2680').",
                },
            },
            "required": ["issue_id"],
            "additionalProperties": False,
        },
        handler=linear_get_issue_relations,
    ),
}


def resolve_tools(names: Iterable[str]) -> List[ToolSpec]:
    """Look up ToolSpec objects for a list of names. Unknown names are logged and skipped."""
    out: List[ToolSpec] = []
    for n in names or []:
        spec = BUILTIN_TOOLS.get(n)
        if spec is None:
            logger.warning("persona references unknown tool: %s", n)
            continue
        out.append(spec)
    return out


async def execute_tool(
    spec: ToolSpec,
    arguments_json: str,
) -> str:
    """Run a tool with JSON-encoded arguments. Always returns a JSON string.

    Errors (bad JSON, handler exceptions) are captured and returned as
    {"error": ...} so the model gets a meaningful tool result rather than the
    dispatch loop blowing up.
    """
    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"bad arguments JSON: {e}"})
    if not isinstance(args, dict):
        return json.dumps({"error": "arguments must be a JSON object"})
    try:
        result = await spec.handler(**args)
    except TypeError as e:
        return json.dumps({"error": f"argument mismatch: {e}"})
    except Exception as e:
        logger.exception("tool %s handler raised", spec.name)
        return json.dumps({"error": f"{type(e).__name__}: {e}"})
    try:
        return json.dumps(result)
    except (TypeError, ValueError):
        return json.dumps({"error": "tool result not JSON-serialisable", "repr": repr(result)[:300]})
