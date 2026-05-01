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


# SAL-3781 (2026-05-01): silent_with_tools early-abort.
#
# Failure mode observed in retry-8 (gpt-oss:120b-cloud, SAL-3596): builder
# called http_get 16 times consecutively, never invoked propose_pr, hit
# MAX_TOOL_ITERATIONS=16 and returned partial. ~50%+ of gpt-oss builder
# dispatches exhibit this pattern under load (registry-documented baseline).
# Each silent attempt wastes ~75s of wall time and a retry-budget slot.
#
# Detection: track consecutive iterations whose dominant tool name is in
# `_NONTERMINAL_LOOP_RISK_TOOLS`. When the counter reaches the threshold
# and no terminal tool (propose_pr / update_pr / pr_review) has fired yet,
# abort early with `silent_with_tools=True` in the result envelope so the
# orchestrator can surface the failure to its retry / fallback-chain logic
# without waiting for the full iteration budget.
#
# The non-terminal set is conservative — only tools we've actually observed
# looping. Other read-only tools (linear_list_*, pr_files_get) are not in
# the set yet to avoid false-positives on legitimate batch-read patterns;
# add them here when a real loop is observed in production.
# SAL-3802: lowered from 4 to 3 after 2026-05-01 fleet refire showed kimi
# and qwen ignore the persona-prompt "at most 2 consecutive http_get" rule
# (PR #335). Substrate must enforce: 3rd consecutive same-tool call now
# trips silent_with_tools detection, aligning the substrate cap with the
# persona prompt's behavioural guidance.
_SILENT_WITH_TOOLS_THRESHOLD = 3
_NONTERMINAL_LOOP_RISK_TOOLS: frozenset[str] = frozenset({"http_get"})
_TERMINAL_TOOL_NAMES: frozenset[str] = frozenset({
    "propose_pr",
    "update_pr",
    "pr_review",
    "github_merge_pr",
})


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
#   4. Fallback: persona.preferred_model, then `_resolve_safe_fallback`
#      (registry's `roles.<role>.last_resort`, else `gpt-oss:120b-cloud`).
#      SAL-3787: previously a hardcoded `"deepseek-v3.2:cloud"` literal.
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


def _peek_kickoff_payload(task: dict) -> dict | None:
    """Return the parsed kickoff-payload dict from `task`, or None.

    Sources, in order:
      1. ``task["description"]`` parsed as JSON when the description starts
         with ``{`` (legacy: orchestrator-parent kickoff bodies are pure
         JSON envelopes).
      2. An HTML-comment-fenced block of the form
         ``<!-- model_routing: {...} -->`` anywhere in the description.
         The orchestrator embeds this on child task bodies to propagate
         the kickoff's per-run model overrides into spawned children.
         A child task body is otherwise plain markdown (the legacy
         JSON-only parse misses it).

    SAL-3670 follow-up (2026-04-30): without (2), the kickoff payload's
    ``model_routing`` / ``builder_fallback_chain`` only fired for the
    orchestrator parent task itself — every child dispatched by the
    orchestrator silently fell through to the registry primary. Closes
    that propagation gap so a chain pinned at kickoff time wins on
    spawned-child attempt-0 dispatches too.

    Returns None for any non-JSON / parse-error / non-dict payload.
    Best-effort — never raises.
    """
    if not isinstance(task, dict):
        return None
    desc = task.get("description")
    if not isinstance(desc, str) or not desc:
        return None

    # (1) Whole-body JSON envelope (kickoff parent).
    if desc.strip().startswith("{"):
        try:
            payload = json.loads(desc)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            return payload

    # (2) Embedded ``<!-- model_routing: {...} -->`` propagation block
    # (orchestrator-injected on child task bodies). Only one such block is
    # parsed; if the operator embeds multiple, the first wins.
    marker = "<!-- model_routing:"
    start = desc.find(marker)
    if start >= 0:
        json_start = start + len(marker)
        end = desc.find("-->", json_start)
        if end > json_start:
            blob = desc[json_start:end].strip()
            try:
                parsed = json.loads(blob)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                return parsed
    return None


def _peek_kickoff_model_override(task: dict, role: str) -> str | None:
    """Return `task['model_routing'][role]` if set, else None.

    Sources, in order:
      * Direct dict path: ``task["model_routing"][role]`` on the in-memory
        task dict (orchestrator-injected in unit tests).
      * Kickoff-payload path via ``_peek_kickoff_payload`` (full-body JSON
        envelope OR embedded ``<!-- model_routing: ... -->`` propagation
        block on child tasks).

    Best-effort — never raises.
    """
    # Direct dict path (orchestrator-injected on child task dicts in tests).
    routing = task.get("model_routing") if isinstance(task, dict) else None
    if isinstance(routing, dict):
        v = routing.get(role)
        if isinstance(v, str) and v:
            return v
    # Kickoff-payload path (parent envelope or child propagation block).
    payload = _peek_kickoff_payload(task)
    if isinstance(payload, dict):
        r2 = payload.get("model_routing")
        if isinstance(r2, dict):
            v = r2.get(role)
            if isinstance(v, str) and v:
                return v
    return None


def _peek_builder_fallback_chain(task: dict) -> list | None:
    """Return ``task["builder_fallback_chain"]`` if set, else None.

    SAL-3670: prior to this helper, attempt-0 dispatch ignored the kickoff
    payload's ``builder_fallback_chain[0]`` and fell straight through to
    the model registry, which would return whatever ``stable_baseline``
    says (often a degraded model). The chain is the operator's
    authoritative wishlist; when present it must win over the registry at
    attempt 0.

    Sources, in order:
      * Direct dict path: ``task["builder_fallback_chain"]`` on the
        in-memory task dict (orchestrator-injected on child task dicts in
        tests).
      * Kickoff-payload path via ``_peek_kickoff_payload`` (full-body JSON
        envelope OR embedded ``<!-- model_routing: ... -->`` propagation
        block on child tasks).

    Returns the list as-is, or None when absent. Best-effort — never raises.
    """
    if not isinstance(task, dict):
        return None
    # Direct dict path.
    chain = task.get("builder_fallback_chain")
    if isinstance(chain, list):
        return chain
    # Kickoff-payload path.
    payload = _peek_kickoff_payload(task)
    if isinstance(payload, dict):
        chain2 = payload.get("builder_fallback_chain")
        if isinstance(chain2, list):
            return chain2
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

    # (1b) SAL-3670: builder_fallback_chain[0] from kickoff payload wins over
    # the registry at attempt 0. Only the builder role consults this; QA /
    # orchestrator have their own routing knobs. Without this tier the
    # operator-supplied chain is ignored on first attempt and dispatch falls
    # through to whatever stable_baseline the registry hands back.
    if role == "builder":
        chain = _peek_builder_fallback_chain(task)
        if chain and isinstance(chain[0], str) and chain[0]:
            logger.info(
                "model picked: role=%s persona=%s ticket=%s model=%s "
                "(attempt 0, source=kickoff_fallback_chain[0])",
                role, getattr(persona, "name", "?"),
                _peek_linear_ticket_for_log(task), chain[0],
            )
            return chain[0]

    # (2) Legacy tag shortcuts.
    if "[tag:strategy]" in title:
        # SAL-3787: was hardcoded "deepseek-v3.2:cloud". deepseek emits
        # Anthropic XML in message.content instead of OpenAI tool_calls,
        # silently breaking tool-using callers (reference_deepseek_tool_use_quirk).
        # Route through the registry-aware safe-fallback resolver so the
        # role's last_resort wins; static safe default is gpt-oss:120b-cloud.
        synthetic_ctx = DispatchContext(persona=getattr(persona, "name", "") or "")
        return _resolve_safe_fallback(synthetic_ctx, "")

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
    # SAL-3787: was hardcoded "deepseek-v3.2:cloud" — see comment at the
    # tag:strategy branch above for why deepseek is unsafe as a fallback.
    synthetic_ctx = DispatchContext(persona=getattr(persona, "name", "") or "")
    return _resolve_safe_fallback(synthetic_ctx, "")


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


# SAL-3782 (2026-05-01): static safe fallback. NEVER deepseek-v3.2:cloud — see
# `reference_deepseek_tool_use_quirk` memory. `gpt-oss:120b-cloud` is the
# registry's canonical `last_resort` for builder/qa/orchestrator/docs roles.
_SAFE_FALLBACK_DEFAULT = "gpt-oss:120b-cloud"


def _resolve_safe_fallback(
    context: Optional["DispatchContext"], primary_model: str
) -> str:
    """Resolve the per-call fallback model when the caller didn't supply one.

    Precedence:
      1. Registry's `roles.<role>.last_resort` for the persona's mapped role,
         when both the persona-to-role mapping and the registry are available.
      2. Static safe default (`gpt-oss:120b-cloud`).

    The legacy `"deepseek-v3.2:cloud"` hardcode is intentionally NOT in the
    chain — deepseek emits Anthropic XML in `message.content` instead of
    OpenAI `tool_calls`, which silently breaks every tool-using caller (see
    PR #327, SAL-3782). If the registry-picked last_resort happens to equal
    `primary_model`, callers (`call()` / `call_with_tools()`) detect that
    and re-raise; this helper does NOT need to special-case it.
    """
    persona_name = (
        context.persona if context is not None else _UNKNOWN_CONTEXT.persona
    )
    role = _PERSONA_ROLE_MAP.get(persona_name)
    if role is not None:
        try:
            from .autonomous_build.model_registry import _registry_role_last_resort
            picked = _registry_role_last_resort(role)
        except Exception as e:  # noqa: BLE001 — registry failures must not crash dispatch
            logger.warning(
                "registry last_resort lookup failed for role=%s: %s; "
                "using static safe default",
                role, e,
            )
            picked = None
        if picked:
            return picked
    return _SAFE_FALLBACK_DEFAULT


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
            # SAL-3782 (2026-05-01): registry-aware fallback. Caller-supplied
            # `fallback_model` still wins when present (persona default or
            # operator override). Otherwise prefer the role's `last_resort`
            # from `model_registry.yaml` over the legacy deepseek hardcode —
            # deepseek emits Anthropic XML in `content` instead of OpenAI
            # tool_calls (reference_deepseek_tool_use_quirk), which silently
            # breaks every tool-using caller. Static safe default is
            # gpt-oss:120b-cloud (the canonical registry last_resort).
            fb = fallback_model or _resolve_safe_fallback(context, model)
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
            # SAL-3782 (2026-05-01): registry-aware fallback (mirror of
            # `call()` above). Tool-use loops are MORE sensitive to the
            # deepseek XML-in-content quirk than plain `call()` because
            # the loop re-feeds tool results, so a fallback to deepseek
            # made tool-using ticket dispatch silently fail on primary
            # error. Prefer the registry role's `last_resort`; static
            # safe default is gpt-oss:120b-cloud.
            fb = fallback_model or _resolve_safe_fallback(context, model)
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
        # SAL-3781: silent_with_tools tracking. `consecutive_loop_tool` is the
        # tool name being repeated; counter resets on any other tool name.
        # `terminal_tool_called` latches True the first time a tool from
        # `_TERMINAL_TOOL_NAMES` fires — once it does, early-abort is disabled
        # for the remainder of the dispatch (the model is making real progress).
        consecutive_loop_tool: str | None = None
        consecutive_loop_count = 0
        terminal_tool_called = False

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
            iteration_tool_names: list[str] = []
            for call in tool_calls:
                call_id = call.get("id") or ""
                fn = call.get("function") or {}
                name = fn.get("name") or ""
                args_json = fn.get("arguments") or "{}"
                iteration_tool_names.append(name)
                if name in _TERMINAL_TOOL_NAMES:
                    terminal_tool_called = True
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

            # SAL-3781: update silent_with_tools counter at end of iteration.
            # An iteration counts as a "loop tick" only when EVERY tool_call in
            # it shares the same name AND that name is in the loop-risk set.
            # Mixed iterations or terminal-tool iterations reset the counter.
            unique_names = set(iteration_tool_names)
            iteration_signature = (
                next(iter(unique_names))
                if len(unique_names) == 1
                else None
            )
            if (
                iteration_signature is not None
                and iteration_signature in _NONTERMINAL_LOOP_RISK_TOOLS
                and not terminal_tool_called
            ):
                if iteration_signature == consecutive_loop_tool:
                    consecutive_loop_count += 1
                else:
                    consecutive_loop_tool = iteration_signature
                    consecutive_loop_count = 1
            else:
                consecutive_loop_tool = None
                consecutive_loop_count = 0

            if consecutive_loop_count >= _SILENT_WITH_TOOLS_THRESHOLD:
                logger.warning(
                    "silent_with_tools detected: '%s' called %d iterations "
                    "consecutively without terminal tool; aborting at "
                    "iteration %d/%d",
                    consecutive_loop_tool,
                    consecutive_loop_count,
                    iteration + 1,
                    effective_cap,
                )
                return {
                    "content": (
                        f"[silent_with_tools detected: '{consecutive_loop_tool}' "
                        f"called {consecutive_loop_count} iterations consecutively "
                        f"without terminal action; aborted early at iteration "
                        f"{iteration + 1}/{effective_cap}]"
                    ),
                    "tokens_in": total_in,
                    "tokens_out": total_out,
                    "model_used": model,
                    "tool_calls": tool_call_log,
                    "iterations": iteration + 1,
                    "silent_with_tools": True,
                    "silent_with_tools_tool": consecutive_loop_tool,
                }

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
