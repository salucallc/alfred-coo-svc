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

import json
import logging
import os
import urllib.request
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional


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
