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
import contextvars
import json
import logging
import os
import re
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional


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
_VALID_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")
_ALLOWED_ORGS = frozenset({"salucallc", "saluca-labs", "cristianxruvalcaba-coder"})


def _git_env() -> Dict[str, str]:
    """Environment for git subprocess calls — identity + token-embedded URL support."""
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "Alfred COO Daemon")
    env.setdefault("GIT_AUTHOR_EMAIL", "alfred-coo@saluca.com")
    env.setdefault("GIT_COMMITTER_NAME", "Alfred COO Daemon")
    env.setdefault("GIT_COMMITTER_EMAIL", "alfred-coo@saluca.com")
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
    token = os.environ.get("GITHUB_TOKEN")
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
        ["git", "clone", "--depth", "50", "--branch", base_branch, clone_url, str(workspace)],
        env=env,
    )
    if rc != 0:
        return {"error": "git clone failed", "stderr": err[:500]}

    rc, _, err = await _run(
        ["git", "checkout", "-B", branch], cwd=workspace, env=env,
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

    rc, _, err = await _run(["git", "add", *written], cwd=workspace, env=env)
    if rc != 0:
        return {"error": "git add failed", "stderr": err[:500]}

    msg = commit_message or f"{title}\n\nAuthored by Alfred COO daemon."
    rc, _, err = await _run(
        ["git", "commit", "-m", msg], cwd=workspace, env=env,
    )
    if rc != 0:
        return {"error": "git commit failed", "stderr": err[:500]}

    rc, _, err = await _run(
        ["git", "push", "-u", "origin", branch], cwd=workspace, env=env,
    )
    if rc != 0:
        return {"error": "git push failed", "stderr": err[:500]}

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


async def http_get(url: str) -> Dict[str, Any]:
    """Read-only GET against an allowlisted URL.

    Returns {status, headers, body, truncated, bytes_read}. The body is clamped
    to HTTP_GET_MAX_BYTES; larger responses arrive truncated with an explicit
    marker. Only text-ish content types are accepted. No auth header is sent
    (the GitHub REST API endpoints reachable here are the public ones).
    """
    ok, reason = _is_allowed_http_url(url)
    if not ok:
        return {"error": f"url rejected: {reason}"}

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "saluca-alfred/1.0 (http_get tool)",
            "Accept": "text/*, application/json;q=0.9, */*;q=0.1",
        },
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
            "fan out work to specialist personas (e.g. delegate a PQ review to "
            "mr-terrific-a, or a revenue question to revenue-pm). The `persona` "
            "argument routes the task; tags are optional free-form labels."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short task title"},
                "description": {"type": "string", "description": "Full task description + context"},
                "persona": {
                    "type": "string",
                    "description": "Persona to route the task to (e.g. 'mr-terrific-a', 'revenue-pm'). Optional.",
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
            "be relative. This is the primary tool for autonomous code changes."
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
                "body": {"type": "string", "description": "PR body (markdown)"},
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
