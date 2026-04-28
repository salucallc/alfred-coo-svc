"""
Model dispatcher with multi-provider fallback support.

AB-21-coo (2026-04-23): all LLM traffic unified through the alfred-chat-stack
gateway's OpenAI-compatible router. Previously three branches bypassed the
gateway for claude-* (direct Anthropic SDK) and openrouter/* (direct
openrouter.ai); now every family funnels through `_call_gateway`, which
stamps the AB-21 observability header contract on every request:

    Authorization: Bearer <autobuild_soulkey>     # from settings, optional
    X-Tiresias-Tenant: <tenant>                   # default "alfred-coo-mc"
    X-Alfred-Persona: <persona>                   # from DispatchContext
    X-Linear-Ticket: <SAL-xxxx>                   # optional
    X-Mesh-Task-Id: <uuid>                        # optional

The gateway's OpenAI-compat layer handles Anthropic routing (requires
ANTHROPIC_API_KEY on the gateway side) and OpenRouter routing (requires
OPENROUTER_API_KEY on the gateway side) transparently. The dispatcher only
knows one endpoint: `{gateway_url}/v1/chat/completions`.

Phase B.3.1 OpenAI-compatible tool-use (`call_with_tools`) remains: the loop
bodies now post to the gateway too and carry the same stamped headers across
every iteration, so the full tool chain is observable end-to-end.
"""

import json
import logging
from dataclasses import dataclass
from typing import Optional

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .tools import ToolSpec, execute_tool, openai_tool_schema


logger = logging.getLogger("alfred_coo.dispatch")


# AB-17-t (2026-04-24): retry wrapper for upstream gateway 5xx flaps.
#
# The Oracle ollama proxy at 172.17.0.1:8185 returns intermittent 500s under
# load (validated 2026-04-24 06:22:13 UTC, mesh task e85d18c0 dispatching
# SAL-2603 ALT-06). Pre-AB-17-t the 500 propagated as a terminal
# HTTPStatusError, the existing fallback layer in `call` / `call_with_tools`
# swapped to `deepseek-v3.2:cloud` on the *next* attempt, and the original
# model never got a second chance. Result: a transient infra flap misclassified
# as a model failure, ticket dispatch dropped silently.
#
# This wrapper sits BELOW the fallback layer: same model, retried up to
# `_INFRA_RETRY_MAX_ATTEMPTS` times on 5xx + connection errors with exponential
# jittered backoff. After exhaustion the original exception is re-raised so the
# existing fallback chain still kicks in. Additive only.
#
# 4xx are NOT retried — those are real client errors (bad model name, malformed
# body, auth) and a retry would just burn cost on a deterministic failure.
_INFRA_RETRY_MAX_ATTEMPTS = 3
_INFRA_RETRY_BASE_SECONDS = 0.5  # 0.5, 1.0, 2.0 + jitter
_INFRA_RETRY_MAX_SECONDS = 4.0


def _is_retryable_infra_error(exc: BaseException) -> bool:
    """Return True iff `exc` is an upstream-flap that warrants a retry.

    Retryable:
      * httpx.HTTPStatusError with 5xx response (transient gateway / proxy)
      * httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
        httpx.RemoteProtocolError, httpx.NetworkError (TCP-level flaps)

    NOT retryable:
      * httpx.HTTPStatusError with 4xx (client error — bad model, malformed
        body, auth — retry just burns cost on a deterministic failure)
      * Any non-httpx exception (logic bugs, JSON decode errors, etc.)
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    if isinstance(exc, (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.RemoteProtocolError,
        httpx.NetworkError,
    )):
        return True
    return False

# AB-17-l (2026-04-24): raised 8 -> 12 after v8-full children truncated mid-investigation
# on tickets with 4+ http_get probes + propose_pr finalisation (mesh task e7f85521,
# warnings at 16:46:18 and 16:49:29 UTC on gpt-oss:120b-cloud). AB-17-i already bounds
# alfred-coo-a investigation to commit within 3 turns after 4 http_gets, so runaway cost
# stays capped.
#
# AB-17-m (2026-04-24): raised 12 -> 20 as the absolute CEILING after v8-full-v2
# (mesh task 6fdf760f) still hit MAX_TOOL_ITERATIONS(12) three times in the first
# 10 min on F07 / F08 / SS-09 scaffolding tickets (17:32 / 17:33 / 17:35 UTC).
# Raw 20-turn budget is NOT handed out indiscriminately — the orchestrator now
# passes a size-aware per-dispatch cap via `iteration_cap_for_size` below
# (size-S=12, size-M=16, size-L=20) and `_tool_loop` clamps any override at
# MAX_TOOL_ITERATIONS in case a caller mis-labels a ticket. Trivial tickets
# keep the 12-turn behaviour; complex scaffolding gets headroom.
MAX_TOOL_ITERATIONS = 20


def iteration_cap_for_size(size_label: str | None) -> int:
    """Return the tool-iteration cap for a ticket's size label.

    AB-17-m (2026-04-24): size-gated caps so trivial tickets don't get
    blanket 20-turn budget. Unknown/None label defaults to size-S cap.
    Ceiling is MAX_TOOL_ITERATIONS regardless of return value (defence
    in depth against a rogue label).
    """
    caps = {"size-s": 12, "size-m": 16, "size-l": 20}
    cap = caps.get((size_label or "").lower(), 12)
    return min(cap, MAX_TOOL_ITERATIONS)


# SAL-2978 (2026-04-25): fix-round dispatches need a bigger budget than the
# initial dispatch because the builder spends extra turns reading the prior PR
# diff + the review feedback before proposing changes. v7aa evidence: SAL-2588
# TIR-06 (size-S, est=1) was dispatched 3 times across the run, every dispatch
# hit MAX_TOOL_ITERATIONS=12. The size-S cap is right for original work; what
# was missing was headroom on respawn. Bump is +4 turns over the size-based
# cap; ceiling at MAX_TOOL_ITERATIONS still applies (size-M fix = 20 = ceiling).
_FIX_ROUND_CAP_BUMP = 4


def iteration_cap_for_dispatch(
    size_label: str | None,
    is_fix_round: bool = False,
) -> int:
    """Return the per-dispatch tool-iteration cap.

    For an initial dispatch this is identical to `iteration_cap_for_size`.
    For a fix-round dispatch the cap is bumped by ``_FIX_ROUND_CAP_BUMP``
    (then clamped at ``MAX_TOOL_ITERATIONS``) so the builder gets headroom
    to read the prior PR + review feedback before pushing fixes.

    The cap is per-dispatch, not per-ticket: every fresh dispatch resets
    the iteration counter to 0 inside ``_tool_loop``. This helper just
    sizes the budget for the current dispatch.

    SAL-2978 (2026-04-25):
      - size-S original: 12, fix-round: 16
      - size-M original: 16, fix-round: 20
      - size-L original: 20, fix-round: 20 (already at ceiling)
    """
    base = iteration_cap_for_size(size_label)
    if is_fix_round:
        return min(base + _FIX_ROUND_CAP_BUMP, MAX_TOOL_ITERATIONS)
    return base


# Sub #62 (2026-04-27) — model registry hot-swap.
#
# Selection precedence at dispatch time:
#   1. Per-kickoff override: `task["model_routing"][<role>]` if the kickoff
#      payload pinned a model for the role. Per-run escape hatch — registry
#      does NOT win over an explicit operator override.
#   2. Per-task tag: legacy `[tag:strategy]` / `[tag:code]` keep working
#      (used by tests + ad-hoc kickoffs). Same as pre-registry behaviour.
#   3. Model registry: load `Z:/_planning/model_registry/registry.yaml`
#      (or the canonical Oracle path) and pick `roles.<role>.primary`,
#      where `<role>` is derived from the persona name.
#   4. Fallback: persona.preferred_model, then "deepseek-v3.2:cloud".
#
# Registry failures (file missing, schema-invalid, role unmapped) ALL fall
# through to (4) without raising; a hot-swap edit cannot crash dispatch.

# Persona-to-registry-role mapping. Anything not in this map falls through
# to the legacy persona.preferred_model path (registry doesn't apply).
_PERSONA_ROLE_MAP: dict[str, str] = {
    "alfred-coo-a": "builder",
    "autonomous-build-a": "builder",
    "hawkman-qa-a": "qa",
    "alfred-coo-orchestrator": "orchestrator",
    # Docs role is currently unused by any persona; left in registry for
    # forward compat with the planned docs-builder persona.
}


def _registry_role_for_persona(persona) -> str | None:
    """Return the registry role for a persona, or None if unmapped."""
    name = getattr(persona, "name", None) or ""
    return _PERSONA_ROLE_MAP.get(name)


def _peek_kickoff_model_override(task: dict, role: str) -> str | None:
    """Return `task['model_routing'][role]` if set, else None.

    The mesh task body (description) is the kickoff JSON for orchestrator
    parents; for child tasks the override is propagated by the orchestrator
    when it builds child task bodies. Best-effort — never raises.
    """
    # Direct dict path (orchestrator-injected on child task dicts in tests).
    routing = task.get("model_routing") if isinstance(task, dict) else None
    if isinstance(routing, dict):
        v = routing.get(role)
        if isinstance(v, str) and v:
            return v
    # JSON-payload path (kickoff parent).
    desc = task.get("description") if isinstance(task, dict) else None
    if isinstance(desc, str) and desc.strip().startswith("{"):
        try:
            payload = json.loads(desc)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            r2 = payload.get("model_routing")
            if isinstance(r2, dict):
                v = r2.get(role)
                if isinstance(v, str) and v:
                    return v
    return None


def select_model(task: dict, persona) -> str:
    """Pick the model for `task` + `persona`.

    See module-level "Sub #62" block for the full precedence ordering. The
    legacy tag-based shortcuts are preserved verbatim so existing test
    fixtures and one-off kickoffs keep working.
    """
    title = task.get("title", "")

    # (1) Per-kickoff override wins, if a role mapping exists.
    role = _registry_role_for_persona(persona)
    if role is not None:
        override = _peek_kickoff_model_override(task, role)
        if override:
            logger.info(
                "model picked: role=%s persona=%s model=%s "
                "(source=kickoff_override)",
                role, getattr(persona, "name", "?"), override,
            )
            return override

    # (2) Legacy tag shortcuts.
    if "[tag:strategy]" in title:
        return "deepseek-v3.2:cloud"
    if "[tag:code]" in title:
        return "qwen3-coder:480b-cloud"

    # (3) Model registry.
    if role is not None:
        try:
            from .autonomous_build.model_registry import _pick_model_for_role
            registry_pick = _pick_model_for_role(role, attempt_n=0)
        except Exception as e:  # noqa: BLE001 — registry failures must not crash dispatch
            logger.warning(
                "model_registry pick failed for role=%s: %s; "
                "falling through to legacy selection",
                role, e,
            )
            registry_pick = None
        if registry_pick:
            logger.info(
                "model picked: role=%s persona=%s ticket=%s model=%s "
                "(attempt 0, source=registry)",
                role, getattr(persona, "name", "?"),
                _peek_linear_ticket_for_log(task), registry_pick,
            )
            return registry_pick

    # (4) Fallback to persona default.
    if persona.preferred_model:
        return persona.preferred_model
    return "deepseek-v3.2:cloud"


# Tiny duplicate of _peek_linear_ticket from main.py — kept here to avoid an
# import cycle (main.py imports dispatch). Pure best-effort log helper.
_LINEAR_TICKET_RE_FALLBACK = __import__("re").compile(r"\b(SAL-\d{1,6})\b")


def _peek_linear_ticket_for_log(task: dict) -> str:
    title = task.get("title") or ""
    m = _LINEAR_TICKET_RE_FALLBACK.search(title)
    return m.group(1) if m else "-"


@dataclass
class DispatchContext:
    """Caller-supplied observability metadata stamped on every gateway call.

    `persona` is always required (stamped as `X-Alfred-Persona`). The other
    two are optional — when set, they produce `X-Linear-Ticket` and
    `X-Mesh-Task-Id` headers respectively, which the AB-21-gw trace
    middleware uses to correlate LLM calls back to the mesh task and the
    Linear ticket that spawned it.
    """

    persona: str
    linear_ticket: Optional[str] = None
    mesh_task_id: Optional[str] = None


# Sentinel context used when a caller doesn't supply one. Emits a single
# warning per process so we notice un-annotated call sites in production
# without spamming the log for every call.
_UNKNOWN_CONTEXT = DispatchContext(persona="unknown")
_warned_missing_context = False


def _default_context() -> DispatchContext:
    global _warned_missing_context
    if not _warned_missing_context:
        logger.warning(
            "dispatch call made without DispatchContext; headers will use "
            "persona=unknown and omit Linear/mesh correlation. "
            "Fix by passing context= at the call site."
        )
        _warned_missing_context = True
    return _UNKNOWN_CONTEXT


def _derive_gateway_base(gateway_url: str, ollama_url: str) -> str:
    """Resolve the gateway base URL.

    Prefers the explicit `gateway_url` setting. Falls back to stripping a
    trailing `/v1` (or `/v1/`) from `ollama_url` so existing Oracle envs
    that only set OLLAMA_URL keep working without a config flip day.
    """
    if gateway_url:
        return gateway_url.rstrip("/")
    base = ollama_url.rstrip("/")
    for suffix in ("/v1", "/v1/"):
        if base.endswith(suffix):
            return base[: -len(suffix)].rstrip("/")
    return base


class Dispatcher:
    def __init__(
        self,
        ollama_url: str,
        anthropic_key: str = "",
        openrouter_key: str = "",
        timeout: float = 300.0,
        *,
        gateway_url: str = "",
        autobuild_soulkey: str = "",
        tiresias_tenant: str = "alfred-coo-mc",
    ):
        # Kept for backwards compatibility with any caller that still passes
        # them; neither is used now that all routing is gateway-side. The
        # gateway owns both keys.
        self.ollama_url = ollama_url.rstrip("/")
        self.anthropic_key = anthropic_key  # unused post AB-21, retained for ctor compat
        self.openrouter_key = openrouter_key  # ditto
        self.timeout = timeout

        # AB-21 gateway plumbing.
        self.gateway_base = _derive_gateway_base(gateway_url, ollama_url)
        self.autobuild_soulkey = autobuild_soulkey
        self.tiresias_tenant = tiresias_tenant

        if not self.autobuild_soulkey:
            logger.warning(
                "AUTOBUILD_SOULKEY not set; dispatch calls will skip the "
                "Authorization header and rely on the gateway's allow-all "
                "policy. Acceptable for pre-AB-21-gw rollout; tighten "
                "before the gateway flips to deny-by-default."
            )

    # ── Header stamping ────────────────────────────────────────────────

    def _build_headers(self, context: DispatchContext) -> dict:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-Tiresias-Tenant": self.tiresias_tenant,
            "X-Alfred-Persona": context.persona or "unknown",
        }
        if self.autobuild_soulkey:
            headers["Authorization"] = f"Bearer {self.autobuild_soulkey}"
        if context.linear_ticket:
            headers["X-Linear-Ticket"] = context.linear_ticket
        if context.mesh_task_id:
            headers["X-Mesh-Task-Id"] = context.mesh_task_id
        return headers

    # ── Gateway call ────────────────────────────────────────────────────

    def _gateway_model(self, model: str) -> str:
        """Return the model string the gateway expects.

        The gateway's OpenAI-compat router dispatches on model prefix:
          * `openrouter/<upstream>` → forwarded to openrouter.ai; the prefix
            is preserved so the gateway can parse `openrouter/` off.
          * `claude-*`              → forwarded to Anthropic via its key.
          * everything else         → Ollama cloud / local.

        This stays a trivial pass-through today; kept as its own method so
        if the gateway contract changes (e.g. wants `anthropic/<model>` for
        claude), we only edit here.
        """
        return model

    async def _call_gateway(
        self,
        model: str,
        messages: list[dict],
        context: Optional[DispatchContext] = None,
        tools: Optional[list[dict]] = None,
    ) -> dict:
        """Single chokepoint for every LLM call made by alfred-coo.

        All three legacy paths (Anthropic direct, Ollama, OpenRouter direct)
        are unified here. Returns the raw OpenAI-compat response JSON so
        callers that need `tool_calls` / `usage` / etc. can introspect.
        """
        ctx = context or _default_context()
        url = f"{self.gateway_base}/v1/chat/completions"
        headers = self._build_headers(ctx)
        body: dict = {
            "model": self._gateway_model(model),
            "messages": messages,
        }
        if tools:
            body["tools"] = tools

        # AB-17-t: retry transient upstream flaps (5xx + connection errors)
        # before surfacing to the existing fallback layer. The retry sits
        # BELOW the fallback chain — fallback still triggers if all attempts
        # exhaust.
        attempt_no = 0
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(_INFRA_RETRY_MAX_ATTEMPTS),
                wait=wait_exponential_jitter(
                    initial=_INFRA_RETRY_BASE_SECONDS,
                    max=_INFRA_RETRY_MAX_SECONDS,
                ),
                retry=retry_if_exception(_is_retryable_infra_error),
                reraise=True,
            ):
                with attempt:
                    attempt_no += 1
                    if attempt_no > 1:
                        logger.warning(
                            "[infra_retry] %s attempt=%d/%d model=%s",
                            url, attempt_no, _INFRA_RETRY_MAX_ATTEMPTS, model,
                        )
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
                        resp = await client.post(url, headers=headers, json=body)
                        resp.raise_for_status()
                        return resp.json()
        except RetryError as re:  # pragma: no cover - reraise=True bypasses this
            raise re.last_attempt.exception() from re
        # Defensive: AsyncRetrying with reraise=True always either returns or
        # raises; this line is unreachable but satisfies type checkers.
        raise RuntimeError("dispatch retry loop exited without result")  # pragma: no cover

    # ── One-shot call ───────────────────────────────────────────────────

    async def call(
        self,
        model: str,
        system: str,
        prompt: str,
        fallback_model: str | None = None,
        context: Optional[DispatchContext] = None,
    ) -> dict:
        try:
            return await self._call_model(model, system, prompt, context=context)
        except Exception:
            fb = fallback_model or "deepseek-v3.2:cloud"
            if fb == model:
                raise
            result = await self._call_model(fb, system, prompt, context=context)
            result["model_used"] = f"{model} -> {fb}"
            return result

    async def _call_model(
        self,
        model: str,
        system: str,
        prompt: str,
        context: Optional[DispatchContext] = None,
    ) -> dict:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        data = await self._call_gateway(model, messages, context=context)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        return {
            "content": msg.get("content", "") or "",
            "tokens_in": usage.get("prompt_tokens", 0),
            "tokens_out": usage.get("completion_tokens", 0),
            "model_used": model,
        }

    # ── Tool-use loop ───────────────────────────────────────────────────

    async def call_with_tools(
        self,
        model: str,
        system: str,
        prompt: str,
        tools: list[ToolSpec],
        fallback_model: str | None = None,
        context: Optional[DispatchContext] = None,
        max_iterations: int | None = None,
    ) -> dict:
        """Multi-turn OpenAI-compatible tool-use loop.

        The model can emit tool_calls; each call is executed and its JSON result
        fed back as a role=tool message; the loop runs until the model emits a
        final message with no tool_calls, or the per-call cap is hit.

        ``max_iterations`` is an optional per-call override (AB-17-m). When None
        the loop uses ``MAX_TOOL_ITERATIONS`` (20). When provided it is clamped
        at ``MAX_TOOL_ITERATIONS`` so a caller can't blow past the ceiling even
        by mistake. The orchestrator uses this to hand out size-gated caps
        (size-S=12, size-M=16, size-L=20) via ``iteration_cap_for_size``.

        Fallback: if the primary model errors at any point, retry the WHOLE loop
        against fallback_model with a fresh message history. This trades some
        wasted tokens for correctness — a partial tool chain on one model cannot
        be meaningfully resumed on another.
        """
        if not tools:
            return await self.call(
                model, system, prompt,
                fallback_model=fallback_model, context=context,
            )

        try:
            return await self._tool_loop(
                model, system, prompt, tools,
                context=context, max_iterations=max_iterations,
            )
        except Exception as e:
            fb = fallback_model or "deepseek-v3.2:cloud"
            if fb == model:
                raise
            logger.warning("tool-use primary %s failed (%s); retrying on %s", model, e, fb)
            result = await self._tool_loop(
                fb, system, prompt, tools,
                context=context, max_iterations=max_iterations,
            )
            result["model_used"] = f"{model} -> {fb}"
            return result

    async def _tool_loop(
        self,
        model: str,
        system: str,
        prompt: str,
        tools: list[ToolSpec],
        context: Optional[DispatchContext] = None,
        max_iterations: int | None = None,
    ) -> dict:
        # AB-17-m: clamp any override at the ceiling. None => default to ceiling.
        if max_iterations is None:
            effective_cap = MAX_TOOL_ITERATIONS
        else:
            effective_cap = min(max_iterations, MAX_TOOL_ITERATIONS)
            # Guard against <=0 overrides collapsing the loop silently. Floor
            # at 1 so at least one model call happens and the size-gating is
            # observable in logs.
            if effective_cap < 1:
                effective_cap = 1

        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        tool_schemas = [openai_tool_schema(t) for t in tools]
        tool_index = {t.name: t for t in tools}
        total_in = 0
        total_out = 0
        tool_call_log: list[dict] = []

        # SAL-2978: explicit log line confirming the iteration counter starts
        # at 0 on every fresh dispatch. The counter is loop-local (the
        # `for iteration in range(...)` below) so it cannot leak across
        # dispatches; this log makes that contract observable in production.
        logger.info(
            "tool-use loop entering; effective_cap=%d iteration_count_reset=True",
            effective_cap,
        )

        for iteration in range(effective_cap):
            data = await self._call_gateway(
                model, messages, context=context, tools=tool_schemas,
            )
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            usage = data.get("usage") or {}
            total_in += usage.get("prompt_tokens", 0)
            total_out += usage.get("completion_tokens", 0)

            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return {
                    "content": msg.get("content", "") or "",
                    "tokens_in": total_in,
                    "tokens_out": total_out,
                    "model_used": model,
                    "tool_calls": tool_call_log,
                    "iterations": iteration + 1,
                }

            messages.append(msg)
            for call in tool_calls:
                call_id = call.get("id") or ""
                fn = call.get("function") or {}
                name = fn.get("name") or ""
                args_json = fn.get("arguments") or "{}"
                spec = tool_index.get(name)
                if spec is None:
                    result_str = json.dumps({"error": f"unknown tool: {name}"})
                else:
                    result_str = await execute_tool(spec, args_json)
                tool_call_log.append({
                    "iteration": iteration,
                    "name": name,
                    "arguments": args_json,
                    "result": result_str,
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": result_str,
                })

        # AB-17-m: log the EFFECTIVE cap (size-aware) not the module ceiling so
        # operators can tell "size-S hit 12" vs "size-L hit 20" at a glance.
        logger.warning(
            "tool-use hit MAX_TOOL_ITERATIONS (%d); returning partial", effective_cap
        )
        return {
            "content": "[tool-use loop exceeded max iterations; partial progress in tool_calls]",
            "tokens_in": total_in,
            "tokens_out": total_out,
            "model_used": model,
            "tool_calls": tool_call_log,
            "iterations": effective_cap,
            "truncated": True,
        }
