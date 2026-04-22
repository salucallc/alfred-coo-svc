"""Structured-output contract + parser for Alfred COO daemon.

Phase B.2: models are asked to emit a JSON envelope of the form

    {
      "summary": "<one to three sentence result summary>",
      "artifacts": [
        {"path": "relative/path.md", "content": "..."},
        ...
      ],
      "follow_up_tasks": [  // optional
        "title of a task to queue next",
        ...
      ]
    }

Parsing is permissive: bare JSON, ```json fenced blocks, and JSON embedded in
prose are all accepted. When parsing fails we return None so callers can fall
back to storing the raw response text.

We never raise from parse_envelope; returning None on malformed output keeps
the dispatch loop resilient.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


OUTPUT_CONTRACT = (
    "\n\nRESPOND AS JSON ONLY, no prose before or after. Use this exact shape:\n"
    "{\n"
    '  "summary": "<one to three sentence summary of what you produced>",\n'
    '  "artifacts": [\n'
    '    {"path": "<relative path under the task workspace>", "content": "<full file content>"}\n'
    "  ],\n"
    '  "follow_up_tasks": ["<optional: titles of follow-up tasks to queue>"]\n'
    "}\n"
    "Rules:\n"
    "- Emit real files when the task asks for an artifact (doc, code, config, plan).\n"
    "- Paths must be relative; no leading slash, no `..`.\n"
    "- If the task is pure analysis with nothing to write, use an empty artifacts array.\n"
    "- Do not wrap the JSON in markdown code fences; return raw JSON.\n"
)


@dataclass
class Envelope:
    summary: str
    artifacts: List[Dict[str, str]]
    follow_up_tasks: List[str]
    raw: str


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def _strip_fences(text: str) -> str:
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1)
    return text


def _find_json_object(text: str) -> Optional[str]:
    """Find the first balanced top-level JSON object in text.

    Scans for an opening brace and returns the substring up to the matching
    close, tracking string quoting so braces inside strings don't confuse the
    balance counter. Returns None if no balanced object is found.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_envelope(raw: str) -> Optional[Envelope]:
    """Best-effort parse of a model response into an Envelope.

    Returns None on any parse failure. Accepts:
      * bare JSON
      * JSON wrapped in ``` or ```json fences
      * JSON embedded in surrounding prose (first balanced top-level object wins)
    """
    if not raw or not isinstance(raw, str):
        return None

    candidate = _strip_fences(raw.strip())
    if not candidate.lstrip().startswith("{"):
        extracted = _find_json_object(candidate)
        if extracted is None:
            return None
        candidate = extracted

    try:
        parsed: Any = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None

    summary = parsed.get("summary")
    if not isinstance(summary, str):
        return None

    artifacts_in = parsed.get("artifacts", [])
    if not isinstance(artifacts_in, list):
        return None
    artifacts: List[Dict[str, str]] = []
    for a in artifacts_in:
        if not isinstance(a, dict):
            return None
        path = a.get("path")
        content = a.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            return None
        artifacts.append({"path": path, "content": content})

    follow_up_in = parsed.get("follow_up_tasks", [])
    if not isinstance(follow_up_in, list):
        return None
    follow_up_tasks = [t for t in follow_up_in if isinstance(t, str)]

    return Envelope(
        summary=summary,
        artifacts=artifacts,
        follow_up_tasks=follow_up_tasks,
        raw=raw,
    )
