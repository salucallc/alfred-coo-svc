"""Fixture schema + supporting dataclasses for Plan M benchmark substrate.

The on-disk form is JSON (see Plan M §3.1). This module owns the in-memory
dataclass representation and the parse/validate step that turns raw JSON into
those objects. It also ships ``compute_prompt_sha(persona_id)`` so fixture
authors (and the M-07 nightly report) can detect persona drift.

Contract: fixtures are append-only and each ``M-MV-*`` move is 1:1 with its
fixture file. The module is intentionally free of I/O side effects other than
the ``from_file`` / ``from_json`` loaders and the prompt-SHA helper that reads
``alfred_coo.persona``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence


# ── Tool-script entries ─────────────────────────────────────────────────────


@dataclass
class ToolScriptEntry:
    """One scripted tool-call interceptor rule.

    Plan M §3.1 shape::

        {
          "when": {"tool": "http_get", "args_match": {"url_contains": "docker-compose.yml"}},
          "return": {"status": 200, "body": {...}}
        }

    The matcher supports a small vocabulary (this is the MVP — extend as
    future fixtures need):

    * ``when.tool``          — exact tool-name match (required).
    * ``when.args_match``    — per-arg predicates:
        - ``{"<arg>": "<literal>"}``        → equality
        - ``{"<arg>_contains": "<s>"}``     → substring-in-arg match
        - ``{"url_contains": "<s>"}``       → shorthand for
          ``{"url_contains": "..."}`` applied to the ``url`` arg.

    ``return_value`` is handed back verbatim as the tool's JSON result.
    """

    tool: str
    args_match: Dict[str, Any] = field(default_factory=dict)
    return_value: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ToolScriptEntry":
        when = raw.get("when") or {}
        tool = when.get("tool")
        if not isinstance(tool, str) or not tool:
            raise ValueError("tool_script entry missing when.tool")
        args_match_raw = when.get("args_match") or {}
        if not isinstance(args_match_raw, dict):
            raise ValueError("tool_script entry when.args_match must be an object")
        return_value = raw.get("return") or {}
        if not isinstance(return_value, dict):
            raise ValueError("tool_script entry return must be an object")
        return cls(
            tool=tool,
            args_match=dict(args_match_raw),
            return_value=dict(return_value),
        )


# ── Pass criterion ──────────────────────────────────────────────────────────


@dataclass
class PassCriterion:
    """Binary pass criterion — Plan M §3.3.

    ``kind`` determines how ``evaluate()`` inspects the transcript.

    v1.0 supports five kinds (Plan M §3.3 table):
      * ``transcript_assert`` — list of {must|not, tool_call|content_regex}
      * ``terminal_form``      — last turn must match one of a set of forms
      * ``tool_call_order``    — ordered supersequence (stub in M-01)
      * ``tool_call_count``    — per-tool bounds (stub in M-01)
      * ``structured_emit``    — parse/regex tool-call args (minimal impl)

    M-01 ships ``transcript_assert`` + ``terminal_form`` + ``structured_emit``
    (minimal). The remaining kinds are parsed but their evaluators raise
    ``NotImplementedError`` when M-05 fixtures exercise them.
    """

    kind: str
    spec: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PassCriterion":
        kind = raw.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError("pass_criterion.kind must be a non-empty string")
        # Capture every non-"kind" field so evaluators can read their spec.
        spec = {k: v for k, v in raw.items() if k != "kind"}
        return cls(kind=kind, spec=spec)


# ── Fixture ─────────────────────────────────────────────────────────────────


# Schema version the loader accepts. Bumped when the on-disk shape changes
# in a non-back-compatible way; fixture authors must sync their JSON.
FIXTURE_SCHEMA_VERSION = 1


@dataclass
class Fixture:
    """One canonical persona-move fixture (Plan M §3.1)."""

    move_id: str
    name: str
    source: str
    persona_id: str
    resolved_prompt_sha: str
    tool_allowlist: List[str]
    messages: List[Dict[str, Any]]
    tool_script: List[ToolScriptEntry]
    turn_limit: int
    pass_criterion: PassCriterion
    scoring_kind: str = "binary"
    scoring_weight: float = 1.0
    tags: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Fixture":
        _require_keys(
            raw,
            [
                "move_id",
                "name",
                "source",
                "persona_id",
                "resolved_prompt_sha",
                "tool_allowlist",
                "messages",
                "tool_script",
                "turn_limit",
                "pass_criterion",
            ],
            ctx="fixture",
        )
        tool_script = [ToolScriptEntry.from_dict(e) for e in raw["tool_script"]]
        pc = PassCriterion.from_dict(raw["pass_criterion"])
        scoring = raw.get("scoring") or {}
        if not isinstance(scoring, dict):
            raise ValueError("fixture.scoring must be an object when present")
        messages = list(raw["messages"])
        for msg in messages:
            if not isinstance(msg, dict) or "role" not in msg or "content" not in msg:
                raise ValueError("each fixture message must have role + content")
        tags = list(raw.get("tags") or [])
        return cls(
            move_id=str(raw["move_id"]),
            name=str(raw["name"]),
            source=str(raw["source"]),
            persona_id=str(raw["persona_id"]),
            resolved_prompt_sha=str(raw["resolved_prompt_sha"]),
            tool_allowlist=[str(t) for t in raw["tool_allowlist"]],
            messages=messages,
            tool_script=tool_script,
            turn_limit=int(raw["turn_limit"]),
            pass_criterion=pc,
            scoring_kind=str(scoring.get("kind", "binary")),
            scoring_weight=float(scoring.get("weight", 1.0)),
            tags=tags,
        )

    @classmethod
    def from_json(cls, text: str) -> "Fixture":
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("fixture JSON must be an object at root")
        return cls.from_dict(raw)

    @classmethod
    def from_file(cls, path: Path | str) -> "Fixture":
        p = Path(path)
        return cls.from_json(p.read_text(encoding="utf-8"))


# ── Score + RunResult ───────────────────────────────────────────────────────


@dataclass
class Score:
    """Per-sample verdict + reason trail."""

    passed: bool
    reasons: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "details": dict(self.details),
        }


@dataclass
class RunResult:
    """Aggregate verdict for N samples of one (fixture, model) pair."""

    move_id: str
    model: str
    samples: List[Score]
    trace_ids: List[str] = field(default_factory=list)

    @property
    def n_samples(self) -> int:
        return len(self.samples)

    @property
    def passes(self) -> int:
        return sum(1 for s in self.samples if s.passed)

    @property
    def pass_rate(self) -> float:
        if not self.samples:
            return 0.0
        return round(self.passes / len(self.samples), 2)

    @property
    def majority_passed(self) -> bool:
        # Tie (e.g. N=2 with 1 pass) resolves to False — we want a clear
        # majority before claiming the candidate handled the move.
        return self.passes > (len(self.samples) / 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "move_id": self.move_id,
            "model": self.model,
            "n_samples": self.n_samples,
            "passes": self.passes,
            "pass_rate": self.pass_rate,
            "majority_passed": self.majority_passed,
            "samples": [s.to_dict() for s in self.samples],
            "trace_ids": list(self.trace_ids),
        }


# ── Persona prompt SHA helper ───────────────────────────────────────────────


def compute_prompt_sha(persona_id: str) -> str:
    """Return ``sha256:<hex>`` of the current resolved prompt for a persona.

    Plan M §8 R-3/R-4: fixtures pin the resolved system prompt by hash so CI
    can detect persona drift. Any edit to ``alfred_coo.persona.BUILTIN_PERSONAS``
    that changes the prompt changes the SHA; the fixture author must confirm
    and re-lock.

    Import of ``alfred_coo.persona`` is deferred to call-time so the schema
    module stays importable in contexts where the persona registry is not
    available (e.g. pure JSON validation).
    """
    from alfred_coo.persona import BUILTIN_PERSONAS  # local import

    persona = BUILTIN_PERSONAS.get(persona_id)
    if persona is None:
        raise KeyError(f"unknown persona_id: {persona_id}")
    digest = hashlib.sha256(persona.system_prompt.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# ── Helpers ─────────────────────────────────────────────────────────────────


def _require_keys(raw: Mapping[str, Any], keys: Sequence[str], *, ctx: str) -> None:
    missing = [k for k in keys if k not in raw]
    if missing:
        raise ValueError(f"{ctx} missing required keys: {', '.join(missing)}")


def fixture_dir() -> Path:
    """Return the on-disk directory that holds canonical fixtures."""
    return Path(__file__).parent / "fixtures"


def load_all_fixtures() -> List[Fixture]:
    """Load every ``M-MV-*.json`` under the fixtures directory, sorted by move_id."""
    out: List[Fixture] = []
    for p in sorted(fixture_dir().glob("M-MV-*.json")):
        out.append(Fixture.from_file(p))
    return out


def load_fixture(move_id: str) -> Fixture:
    """Load a specific fixture by move id. Raises FileNotFoundError if missing."""
    path = fixture_dir() / f"{move_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"no fixture for move_id={move_id!r} at {path}")
    return Fixture.from_file(path)
