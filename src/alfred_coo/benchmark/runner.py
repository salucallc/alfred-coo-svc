"""Fixture interpreter, tool-script interceptor, transcript evaluator.

Plan M §3.2 runner protocol, §3.3 pass-criterion kinds.

Three public surfaces:

* ``FixtureScript``   — object that intercepts tool-call name + args, matches
  against the fixture's ``tool_script`` entries, and returns the scripted
  response. Unknown-tool or no-match → raises ``UnscriptedToolCall``.

* ``evaluate(fixture, transcript) -> Score`` — binary evaluator. Routes on
  ``fixture.pass_criterion.kind`` and inspects the transcript. v1.0 supports
  ``transcript_assert``, ``terminal_form``, and a minimal ``structured_emit``.
  ``tool_call_order`` / ``tool_call_count`` raise ``NotImplementedError`` (M-05
  follow-up).

* ``run_fixture(fixture, model, ...)`` — dispatches the fixture through the
  ``alfred_coo.dispatch.Dispatcher`` against the configured gateway, installs
  the ``FixtureScript`` as a tool interceptor for the tools named in
  ``fixture.tool_allowlist``, and records N samples into a ``RunResult``. The
  gateway URL is picked up from ``ALFRED_BENCH_GATEWAY_URL`` (preferred) or
  falls back to the standard ``alfred_coo.config`` settings path.

The runner reuses ``alfred_coo.tools.BUILTIN_TOOLS``: for each allowed tool we
substitute a handler that calls into ``FixtureScript.invoke``. This keeps the
tool schemas (which the model sees) identical to production while making the
effects deterministic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .schema import Fixture, PassCriterion, RunResult, Score, ToolScriptEntry


logger = logging.getLogger("alfred_coo.benchmark.runner")


# ── Exceptions ──────────────────────────────────────────────────────────────


class UnscriptedToolCall(RuntimeError):
    """Raised when a fixture run observes a tool call that the fixture's
    ``tool_script`` does not match. Plan M §3.2: "an uncovered tool call →
    fixture fails with reason unexpected_tool_call".

    The runner catches this per-sample and converts it into a failed
    ``Score`` with reason ``unexpected_tool_call``; tests can also observe
    it directly via ``FixtureScript.invoke``.
    """

    def __init__(self, tool: str, arguments: Mapping[str, Any]):
        self.tool = tool
        self.arguments = dict(arguments)
        super().__init__(f"unscripted tool call: {tool}(args={arguments!r})")


# ── Fixture-script interceptor ──────────────────────────────────────────────


class FixtureScript:
    """Match a tool call against a fixture's scripted entries and return the
    scripted response (a JSON-serialisable dict). Raises ``UnscriptedToolCall``
    when no entry matches.

    Matching rules (Plan M §3.1 matcher grammar):

    * ``when.tool``              — tool name (exact).
    * ``when.args_match``        — each key is a predicate on arguments:
        - key ``foo``             literal equality on ``arguments["foo"]``.
        - key ``foo_contains``    substring check: value must appear in
          ``str(arguments["foo"])``.
        - key ``url_contains``    convenience shortcut: substring check
          against ``arguments["url"]``.

    The entries are tried in order; first match wins.
    """

    def __init__(self, entries: Sequence[ToolScriptEntry]):
        self._entries = list(entries)
        self.call_log: List[Dict[str, Any]] = []

    def invoke(self, tool: str, arguments: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        args = dict(arguments or {})
        self.call_log.append({"tool": tool, "arguments": args})
        for entry in self._entries:
            if entry.tool != tool:
                continue
            if self._args_match(entry.args_match, args):
                return dict(entry.return_value)
        raise UnscriptedToolCall(tool, args)

    @staticmethod
    def _args_match(predicate: Mapping[str, Any], arguments: Mapping[str, Any]) -> bool:
        for key, expected in predicate.items():
            if key.endswith("_contains"):
                arg_name = key[: -len("_contains")]
                actual = arguments.get(arg_name, "")
                if not isinstance(expected, str):
                    return False
                if expected not in str(actual):
                    return False
            else:
                if arguments.get(key) != expected:
                    return False
        return True


# ── Transcript shape ────────────────────────────────────────────────────────


# A transcript is a dict with at minimum:
#   {
#     "content": "<final model text, or '' if tool loop closed with no text>",
#     "tool_calls": [
#        {"iteration": int, "name": str, "arguments": str_json, "result": str_json},
#        ...
#     ],
#     "messages": [... OpenAI-compat message list, optional ...],
#   }
#
# Matches the shape returned by ``Dispatcher.call_with_tools``. We keep the
# evaluator defensive — missing fields fall back to empty/defaults rather
# than raising — because fixtures must fail *safely* not crash.


# ── Evaluator ───────────────────────────────────────────────────────────────


def evaluate(fixture: Fixture, transcript: Mapping[str, Any]) -> Score:
    """Apply a fixture's pass criterion to a transcript. Returns a ``Score``.

    Never raises on "the model did something weird"; it just returns
    ``passed=False`` with an explanatory reason. It DOES raise
    ``NotImplementedError`` for criterion kinds whose evaluator is not yet
    implemented, because that's a fixture-authoring bug, not a transcript
    bug.
    """
    kind = fixture.pass_criterion.kind
    if kind == "transcript_assert":
        return _eval_transcript_assert(fixture.pass_criterion, transcript)
    if kind == "terminal_form":
        return _eval_terminal_form(fixture.pass_criterion, transcript)
    if kind == "structured_emit":
        return _eval_structured_emit(fixture.pass_criterion, transcript)
    if kind in ("tool_call_order", "tool_call_count"):
        # Kept deliberately as NotImplementedError so fixture authors
        # discover the gap at first run; follow-up ticket will implement.
        raise NotImplementedError(
            f"pass_criterion.kind={kind!r} evaluator is not yet implemented "
            "(see Plan M §5.1 M-05 follow-up)"
        )
    raise ValueError(f"unknown pass_criterion.kind: {kind!r}")


# --- transcript_assert ------------------------------------------------------

def _eval_transcript_assert(pc: PassCriterion, transcript: Mapping[str, Any]) -> Score:
    """Plan M §3.3 ``transcript_assert``::

        {
          "kind": "transcript_assert",
          "asserts": [
            {"not": "linear_create_issue"},
            {"not": {"content_regex": "\\.yaml"}},
            {"must": {"tool_call": "propose_pr"}}
          ]
        }

    Shape of each assert clause:
      * ``"must" | "not": "<tool_name>"``                  → tool-call presence.
      * ``"must" | "not": {"tool_call": "<tool_name>"}``   → same, explicit form.
      * ``"must" | "not": {"content_regex": "<regex>"}``   → regex over
        transcript content (combined final-text + any assistant-message text).
    """
    asserts = pc.spec.get("asserts") or []
    if not isinstance(asserts, list):
        return Score(passed=False, reasons=["transcript_assert.asserts must be a list"])

    tool_names = _transcript_tool_names(transcript)
    content_blob = _transcript_content_blob(transcript)

    reasons: List[str] = []
    for clause in asserts:
        if not isinstance(clause, dict) or not clause:
            reasons.append(f"malformed assert clause: {clause!r}")
            continue
        # There's exactly one key: "must" or "not".
        if "must" in clause:
            polarity = "must"
            target = clause["must"]
        elif "not" in clause:
            polarity = "not"
            target = clause["not"]
        else:
            reasons.append(f"assert missing must/not: {clause!r}")
            continue

        observed, human = _observe(target, tool_names, content_blob)

        if polarity == "must" and not observed:
            reasons.append(f"must clause failed: {human} not observed")
        elif polarity == "not" and observed:
            reasons.append(f"not clause failed: {human} was observed")

    return Score(
        passed=not reasons,
        reasons=reasons,
        details={
            "tool_names": list(tool_names),
            "content_len": len(content_blob),
        },
    )


def _observe(target: Any, tool_names: Sequence[str], content_blob: str) -> tuple[bool, str]:
    """Interpret one assert target. Returns (observed, human-readable form)."""
    if isinstance(target, str):
        # Shorthand: "tool_name".
        return target in tool_names, f"tool_call:{target}"
    if isinstance(target, dict):
        if "tool_call" in target:
            name = target["tool_call"]
            return name in tool_names, f"tool_call:{name}"
        if "content_regex" in target:
            pattern = target["content_regex"]
            try:
                match = re.search(pattern, content_blob) is not None
            except re.error as e:
                return False, f"content_regex:{pattern!r} (INVALID REGEX: {e})"
            return match, f"content_regex:{pattern!r}"
    return False, f"unknown target: {target!r}"


# --- terminal_form ---------------------------------------------------------

def _eval_terminal_form(pc: PassCriterion, transcript: Mapping[str, Any]) -> Score:
    """Plan M §3.3 ``terminal_form``::

        {
          "kind": "terminal_form",
          "forms": [
            {"tool_call": "pr_review", "required_args": ["state"]},
            {"regex": "\\b(APPROVE|REQUEST_CHANGES|COMMENT)\\b"}
          ]
        }

    The last turn must match AT LEAST ONE form. "Last turn" is defined as:
      * the final tool_call, if the transcript ended with one, OR
      * the final content string, if the transcript ended with prose.

    For simplicity v1.0 checks each form against the whole transcript
    (last tool_call + final content); the final turn is the only place
    either can appear in our tool-use loop, so false positives from
    "tool called earlier" vs "tool called at end" are not realistic given
    the short fixture transcripts.
    """
    forms = pc.spec.get("forms") or []
    if not isinstance(forms, list) or not forms:
        return Score(passed=False, reasons=["terminal_form.forms must be a non-empty list"])

    content_blob = _transcript_content_blob(transcript)
    tool_calls = transcript.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        tool_calls = []
    last_tool_call = tool_calls[-1] if tool_calls else None

    matched_form: Optional[Dict[str, Any]] = None
    for form in forms:
        if not isinstance(form, dict):
            continue

        if "tool_call" in form:
            want = form["tool_call"]
            if last_tool_call and last_tool_call.get("name") == want:
                required_args = form.get("required_args") or []
                args = _parse_args(last_tool_call.get("arguments"))
                if all(k in args for k in required_args):
                    matched_form = form
                    break
            # Also accept a pr_review tool call anywhere in the transcript
            # when a strict "last turn" check would force a match that the
            # loop wouldn't have produced (pr_review finalises the loop).
            if any(isinstance(tc, dict) and tc.get("name") == want for tc in tool_calls):
                required_args = form.get("required_args") or []
                # Find the pr_review call and check its args.
                for tc in tool_calls:
                    if isinstance(tc, dict) and tc.get("name") == want:
                        args = _parse_args(tc.get("arguments"))
                        if all(k in args for k in required_args):
                            matched_form = form
                            break
                if matched_form is not None:
                    break

        if "regex" in form:
            pattern = form["regex"]
            try:
                if re.search(pattern, content_blob):
                    matched_form = form
                    break
            except re.error:
                continue

    if matched_form is None:
        return Score(
            passed=False,
            reasons=[
                "terminal_form matched no form; last turn was "
                f"{'tool_call=' + last_tool_call.get('name', '?') if last_tool_call else 'prose-only'}"
            ],
            details={
                "last_tool_call": last_tool_call.get("name") if last_tool_call else None,
                "content_len": len(content_blob),
            },
        )
    return Score(
        passed=True,
        reasons=[],
        details={"matched_form": matched_form},
    )


# --- structured_emit -------------------------------------------------------

def _eval_structured_emit(pc: PassCriterion, transcript: Mapping[str, Any]) -> Score:
    """Minimal ``structured_emit`` evaluator (Plan M §3.3).

    Spec shape::

        {
          "kind": "structured_emit",
          "tool": "propose_pr",
          "arg_regexes": {"branch": "^feature/sal-\\d+", "body": "### Accept"},
          "min_calls": 1,
          "max_calls": 1
        }

    Checks that exactly ``min_calls..max_calls`` calls to ``tool`` were made
    and that each call's serialised-arguments JSON satisfies every regex in
    ``arg_regexes`` (regex is matched against the per-arg string OR the full
    arguments-JSON when the arg isn't present as a top-level key).
    """
    tool_name = pc.spec.get("tool")
    if not isinstance(tool_name, str) or not tool_name:
        return Score(passed=False, reasons=["structured_emit.tool required"])
    min_calls = int(pc.spec.get("min_calls", 1))
    max_calls = int(pc.spec.get("max_calls", 1))
    arg_regexes = pc.spec.get("arg_regexes") or {}
    if not isinstance(arg_regexes, dict):
        return Score(passed=False, reasons=["structured_emit.arg_regexes must be an object"])

    tool_calls = transcript.get("tool_calls") or []
    matching = [
        tc for tc in tool_calls
        if isinstance(tc, dict) and tc.get("name") == tool_name
    ]
    if not (min_calls <= len(matching) <= max_calls):
        return Score(
            passed=False,
            reasons=[
                f"structured_emit: expected {min_calls}..{max_calls} call(s) to "
                f"{tool_name!r}, observed {len(matching)}"
            ],
        )

    reasons: List[str] = []
    for call in matching:
        args = _parse_args(call.get("arguments"))
        raw_args = call.get("arguments") or "{}"
        for arg_name, pattern in arg_regexes.items():
            target_text = str(args.get(arg_name, raw_args))
            try:
                if not re.search(pattern, target_text):
                    reasons.append(
                        f"structured_emit: arg {arg_name!r} failed regex {pattern!r}"
                    )
            except re.error as e:
                reasons.append(
                    f"structured_emit: arg {arg_name!r} regex invalid: {e}"
                )

    return Score(passed=not reasons, reasons=reasons, details={"matches": len(matching)})


# ── Transcript inspection helpers ──────────────────────────────────────────


def _transcript_tool_names(transcript: Mapping[str, Any]) -> List[str]:
    out: List[str] = []
    for tc in transcript.get("tool_calls") or []:
        if isinstance(tc, dict) and isinstance(tc.get("name"), str):
            out.append(tc["name"])
    return out


def _transcript_content_blob(transcript: Mapping[str, Any]) -> str:
    """Flatten all assistant-visible text into one string for regex scanning."""
    parts: List[str] = []
    final_content = transcript.get("content")
    if isinstance(final_content, str):
        parts.append(final_content)
    for msg in transcript.get("messages") or []:
        if isinstance(msg, dict):
            c = msg.get("content")
            if isinstance(c, str):
                parts.append(c)
    return "\n".join(parts)


def _parse_args(raw: Any) -> Dict[str, Any]:
    """Best-effort arguments parser — OpenAI-compat tool-call arguments are a
    JSON string; older mock transcripts may pre-parse to a dict."""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


# ── run_fixture — live dispatch ─────────────────────────────────────────────


# Dispatch plumbing is deliberately kept minimal for M-01/02. The canonical
# full-featured invoker (with proxy wiring, trace tagging, live-replay) lands
# in M-03 (benchmark-svc). What this module ships is enough to let a developer
# smoke-test one fixture/one model on their workstation.


@dataclass
class _BenchmarkToolResult:
    """Bag wrapping a FixtureScript + per-call log for one sample run."""

    script: FixtureScript
    captured: List[Dict[str, Any]]


def _build_bench_tools(fixture: Fixture, script: FixtureScript):
    """Return a list[ToolSpec] whose handlers route through ``script``.

    We only include tools that are actually in the fixture's allowlist; other
    tools the persona normally has access to are withheld so the runner is
    fully deterministic.
    """
    from alfred_coo.tools import BUILTIN_TOOLS, ToolSpec  # local import

    bench_tools = []
    for name in fixture.tool_allowlist:
        spec = BUILTIN_TOOLS.get(name)
        if spec is None:
            logger.warning("fixture %s allowlists unknown tool %r; skipping", fixture.move_id, name)
            continue

        async def _bench_handler(__name=name, __script=script, **kwargs):
            try:
                return __script.invoke(__name, kwargs)
            except UnscriptedToolCall as e:
                # Convert into a tool result the model can see; the runner's
                # per-sample loop will inspect script.call_log after dispatch
                # and decide pass/fail. We still log for observability.
                logger.info("fixture %s: unscripted call to %s(%s)", fixture.move_id, __name, kwargs)
                return {"error": "unscripted_tool_call", "tool": __name, "arguments": dict(kwargs)}

        bench_tools.append(
            ToolSpec(
                name=spec.name,
                description=spec.description,
                parameters=spec.parameters,
                handler=_bench_handler,
            )
        )
    return bench_tools


async def run_fixture(
    fixture: Fixture,
    model: str,
    *,
    n_samples: int = 3,
    gateway_url: Optional[str] = None,
    autobuild_soulkey: Optional[str] = None,
    tiresias_tenant: str = "alfred-coo-mc",
    dispatcher=None,
) -> RunResult:
    """Run a fixture against one model N times. Returns a ``RunResult``.

    Plan M §3.2::

        for sample in range(n_samples):
            trace_id = proxy.dispatch(persona, model, messages, tools,
                                      tool_interceptor, trace_tags=...)
            score = evaluate(fixture.pass_criterion, transcript)

    ``dispatcher`` is an optional injection seam for tests. In production the
    caller typically leaves it None and the runner builds a real
    ``alfred_coo.dispatch.Dispatcher`` from env.
    """
    from alfred_coo.dispatch import Dispatcher, DispatchContext  # local import

    if dispatcher is None:
        gateway = gateway_url or os.environ.get("ALFRED_BENCH_GATEWAY_URL", "")
        soulkey = autobuild_soulkey or os.environ.get("AUTOBUILD_SOULKEY", "")
        ollama = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
        dispatcher = Dispatcher(
            ollama_url=ollama,
            gateway_url=gateway,
            autobuild_soulkey=soulkey,
            tiresias_tenant=tiresias_tenant,
        )

    samples: List[Score] = []
    trace_ids: List[str] = []

    for sample_idx in range(n_samples):
        run_id = f"{fixture.move_id}-{sample_idx}-{uuid.uuid4().hex[:8]}"
        trace_ids.append(run_id)

        script = FixtureScript(fixture.tool_script)
        bench_tools = _build_bench_tools(fixture, script)

        system_msg = None
        user_msg = None
        for m in fixture.messages:
            if m["role"] == "system" and system_msg is None:
                system_msg = m["content"]
            elif m["role"] == "user" and user_msg is None:
                user_msg = m["content"]
        if system_msg is None or user_msg is None:
            samples.append(Score(
                passed=False,
                reasons=["fixture must have at least one system and one user message"],
            ))
            continue

        ctx = DispatchContext(
            persona=fixture.persona_id,
            linear_ticket=f"BENCH-{fixture.move_id}-{run_id}",
        )

        try:
            result = await dispatcher.call_with_tools(
                model=model,
                system=system_msg,
                prompt=user_msg,
                tools=bench_tools,
                context=ctx,
            )
        except Exception as e:
            samples.append(Score(
                passed=False,
                reasons=[f"dispatch failed: {type(e).__name__}: {e}"],
                details={"run_id": run_id},
            ))
            continue

        transcript = {
            "content": result.get("content", "") or "",
            "tool_calls": result.get("tool_calls") or [],
            "messages": [],
        }
        # Surface unscripted calls back into the Score even though the model
        # saw an {error: unscripted_tool_call} stub.
        unscripted = [
            c for c in script.call_log
            if not any(
                e.tool == c["tool"]
                and FixtureScript._args_match(e.args_match, c["arguments"])
                for e in fixture.tool_script
            )
        ]
        if unscripted:
            samples.append(Score(
                passed=False,
                reasons=[
                    f"unscripted tool call(s): "
                    + ", ".join(sorted({c["tool"] for c in unscripted}))
                ],
                details={"unscripted": unscripted, "run_id": run_id},
            ))
            continue

        score = evaluate(fixture, transcript)
        score.details.setdefault("run_id", run_id)
        samples.append(score)

    return RunResult(
        move_id=fixture.move_id,
        model=model,
        samples=samples,
        trace_ids=trace_ids,
    )
