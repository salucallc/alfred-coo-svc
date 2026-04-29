"""Main async loop for alfred-coo-svc v0.

Polls soul-svc mesh_tasks, claims tasks with [persona:xxx] or [unified-plan-wave-1]
markers, loads recent soul memory as context, dispatches to the selected cloud
model, writes result + memory back, heartbeats, sleeps, repeats. Surviving error
handler on the loop body so one bad task never kills the daemon.

Long-running orchestrators: a persona with `handler` set opts out of the
one-shot dispatch path. After a successful claim we resolve the handler class
from `alfred_coo.autonomous_build.orchestrator` (and siblings), instantiate it,
spawn it as a detached asyncio.Task, and continue polling. See plan
Z:/_planning/v1-ga/F_autonomous_build_persona.md §1.
"""

import asyncio
import importlib
import json
import logging
import re
from typing import Optional

import uvicorn

from . import config, log, dispatch, health
from . import cockpit_router
from .mesh import MeshClient, parse_persona_tag
from .soul import SoulClient
from .persona import Persona, get_persona
from .dispatch import (
    Dispatcher,
    DispatchContext,
    iteration_cap_for_dispatch,
)
from .structured import OUTPUT_CONTRACT, parse_envelope
from .artifacts import write_artifacts
from .tools import resolve_tools, set_current_task_id, reset_current_task_id
from .persona_github import (
    set_current_persona,
    reset_current_persona,
    log_identity_summary,
)


logger = logging.getLogger(__name__)

# AB-21: Linear ticket identifier pattern — "SAL-" (or similar team prefix)
# followed by 1-6 digits. Used by `_peek_linear_ticket` to pull a correlation
# id out of the mesh task title for the observability header contract.
_LINEAR_TICKET_RE = re.compile(r"\b([A-Z]{2,5}-\d{1,6})\b")

# SAL-3038 / SAL-3070 (2026-04-28): Linear label name and terminal-state
# names that disqualify a freshly-claimed mesh task from dispatch.
# Mirrors `HUMAN_ASSIGNED_LABEL` in autonomous_build/orchestrator.py — the
# orchestrator path already enforces this gate on hydrated `Ticket`
# objects (orchestrator.py:3076-3084, PR #171). The bare poll loop here
# never goes through hydration, so the label was being silently
# bypassed. Keep this constant in sync if the orchestrator one moves.
HUMAN_ASSIGNED_LABEL = "human-assigned"
LINEAR_TERMINAL_STATES = frozenset({"done", "cancelled", "canceled", "duplicate"})

# AB-17-m: regex for the `Size: X` line the autonomous-build orchestrator
# writes into the child task body via `_child_task_body` (see
# `autonomous_build/orchestrator.py`). Matches `Size: S`, `Size: M`, `Size: L`
# and the three-letter `Size: XS` / `Size: XL` variants, anchored to a line
# start so it doesn't false-match arbitrary prose. Case-insensitive.
_SIZE_LINE_RE = re.compile(
    r"^\s*Size:\s*(XS|S|M|L|XL)\b",
    re.IGNORECASE | re.MULTILINE,
)

# AB-17-m: label-style size tag (`size-S`, `size-l`, etc.) as a fallback if the
# `Size:` line is absent — the orchestrator labels Linear issues with these
# and they sometimes surface in the mesh task title.
_SIZE_LABEL_RE = re.compile(
    r"\bsize-(xs|s|m|l|xl)\b",
    re.IGNORECASE,
)

# AB-17-m: the personas that are BUILDERS, not reviewers. Builders get
# size-gated iteration caps so complex scaffolding tickets (size-L) get room
# to breathe while trivial tickets stay at the size-S default (12 turns).
# Reviewers (hawkman-qa-a) use the default cap because review loops are
# short (1-3 turns) and blanket-applying the 20-turn ceiling would only
# inflate cost on noisy reviewer misfires.
_BUILDER_PERSONAS = frozenset({"alfred-coo-a", "autonomous-build-a"})

# SAL-2978 (2026-04-25): fix-round dispatch detector.
#
# The autonomous-build orchestrator's `_respawn_for_fix_round` (in
# `autonomous_build/orchestrator.py`) renders the respawn task title as:
#
#     "[persona:alfred-coo-a] [wave-N] [<epic>] <ident><code> — fix: round N (...)"
#
# We match the literal "— fix: round " substring (the em-dash is U+2014, the
# orchestrator emits the same character) so initial dispatches and fix-round
# respawns can be distinguished without a separate flag in the task body.
# Falls back to ASCII " - fix: round " for forward-compat in case the
# orchestrator ever stops using the em-dash.
_FIX_ROUND_TITLE_RE = re.compile(
    r"(?:—|--|-)\s*fix:\s*round\s+\d+",
    re.IGNORECASE,
)


def _is_fix_round_dispatch(task: dict) -> bool:
    """Best-effort detection of a fix-round respawn task.

    Returns ``True`` iff the task title carries the orchestrator's
    "— fix: round N" marker. Falls back to ``False`` on missing title or
    no match.

    Used by `_builder_iteration_cap` to bump the size-gated cap by 4 on
    fix-round dispatches so the builder has headroom to read the prior PR
    + review feedback before pushing fixes (see SAL-2978).
    """
    title = task.get("title") or ""
    if not title:
        return False
    return bool(_FIX_ROUND_TITLE_RE.search(title))


def _peek_size_label(task: dict) -> Optional[str]:
    """Best-effort extraction of a ticket size label from the mesh task.

    Returns one of ``size-s``/``size-m``/``size-l``/``size-xs``/``size-xl``
    (lowercase, prefixed) when the autonomous-build orchestrator has stamped
    the ticket with a `Size:` line in the body or a `size-*` label in the
    title. Returns ``None`` otherwise — callers should treat that as
    "unknown, use the default cap".

    Never raises: a missing size must not block dispatch.
    """
    for field in ("description", "title"):
        txt = task.get(field) or ""
        if not txt:
            continue
        m = _SIZE_LINE_RE.search(txt)
        if m:
            return f"size-{m.group(1).lower()}"
        m = _SIZE_LABEL_RE.search(txt)
        if m:
            return f"size-{m.group(1).lower()}"
    return None


def _builder_iteration_cap(persona_name: str, task: dict) -> Optional[int]:
    """Return the size-gated tool-iteration cap for a BUILDER dispatch.

    AB-17-m: only builders (alfred-coo-a / autonomous-build-a) receive a
    size-aware cap. Reviewers (hawkman-qa-a) and any other persona get
    ``None`` so ``dispatcher.call_with_tools`` falls back to the module-level
    ``MAX_TOOL_ITERATIONS`` ceiling. Kept as a pure helper so tests can
    exercise the mapping without spinning up a full dispatcher.

    SAL-2978 (2026-04-25): fix-round dispatches now get a +4 bump over the
    size-based cap (clamped at MAX_TOOL_ITERATIONS). v7aa evidence: SAL-2588
    TIR-06 (size-S, est=1) hit MAX_TOOL_ITERATIONS=12 on every dispatch
    because the size-S cap was right for original work but didn't account
    for the extra turns a fix-round spends reading the prior PR + review
    feedback before pushing fixes.
    """
    if persona_name not in _BUILDER_PERSONAS:
        return None
    size = _peek_size_label(task)
    is_fix_round = _is_fix_round_dispatch(task)
    return iteration_cap_for_dispatch(size, is_fix_round=is_fix_round)


# Module-level registry of long-running orchestrator tasks, keyed by the mesh
# task id that triggered the spawn. Exposed for tests + ops introspection.
# Entries are not cleaned up automatically in v0; on daemon restart the
# orchestrator is expected to reconcile from soul memory state (plan F §2 R2).
_running_orchestrators: dict[str, asyncio.Task] = {}


# AB-09: Layer-2 zombie guard. Maps linear_project_id -> mesh_task_id for any
# orchestrator currently running in THIS daemon. Prevents two orchestrators
# from spawning against the same Linear project when the daemon restarts and
# re-claims a kickoff whose server-side claim lease has expired.
#
# Layer 1 (soul-svc claim heartbeat / idempotent re-claim) is deferred to
# AB-11; G3 verified 2026-04-23 that soul-svc's claim endpoint returns 409
# on same-holder re-claim rather than refreshing the lease, so this local
# guard is the only defence until that server-side fix lands.
_orchestrators_by_project: dict[str, str] = {}


def _is_already_running_orchestrator(task_id: str) -> bool:
    """SAL-2952: pre-claim check for the main poll loop.

    Returns True if `task_id` is the id of an orchestrator parent kickoff
    task that this process is already running (asyncio.Task is in
    `_running_orchestrators` and has not finished). The main loop must skip
    such tasks BEFORE issuing a `mesh.claim` call so it does not generate
    its own `duplicate_kickoff` cancel signal.

    Background: `mesh.list_pending` can surface this daemon's own running
    orchestrator parent task because soul-svc's claim lease can expire on a
    long kickoff (SAL-2890 evidence: 50 s cadence, 57 events over 46 min in
    v7y wave-1). Without this guard the loop would re-claim, then the AB-09
    Layer-2 zombie guard inside `_spawn_long_running_handler` would mark the
    re-claimed task `failed` with `duplicate_kickoff:` reason. PR #92's
    `_check_cancel_signal` filter then ignores the self-inflicted cancel,
    but the round-trip still burns a main-loop tick (and a `mesh.claim`
    API call) that should have gone to dispatcher work — starving the
    dispatcher (9 of 12 wave-1 builders never dispatched in v7y).

    The PR #92 cancel-handler filter remains as defence-in-depth for any
    code path that still slips through (e.g. another daemon, a manual
    operator action, a different claim site we haven't audited yet).
    """
    orch_task = _running_orchestrators.get(task_id)
    if orch_task is None:
        return False
    return not orch_task.done()


# Candidate modules searched by `_resolve_handler`. Order matters: first match
# wins. Kept as a list so future long-running handlers (e.g. a different
# persona class) can slot in without touching the resolver.
_HANDLER_MODULES: tuple[str, ...] = (
    "alfred_coo.autonomous_build.orchestrator",
)


def _peek_kickoff_project_id(task: dict) -> Optional[str]:
    """Best-effort extraction of `linear_project_id` from a kickoff task's
    description. Returns None if the description is missing, not JSON, or
    the key is absent. Never raises: a malformed payload must not crash the
    spawn path — it just falls back to the per-task-id tracking that was in
    place before AB-09.
    """
    desc = task.get("description") or ""
    if not desc:
        return None
    try:
        payload = json.loads(desc)
    except (ValueError, TypeError) as e:
        logger.debug(
            "kickoff description is not JSON; skipping project-id peek "
            "(task=%s, err=%s)",
            task.get("id"),
            e,
        )
        return None
    if not isinstance(payload, dict):
        return None
    pid = payload.get("linear_project_id")
    if pid is None:
        return None
    return str(pid)


# SAL-3140 (2026-04-28): Builder propose_pr enforcement gate.
#
# Builders ([persona:alfred-coo-a] + [tag:code] in title) MUST invoke at least
# one of these tools per dispatch — propose_pr (happy path), update_pr (fix
# rounds), or linear_create_issue (escalate path). The persona prompt at
# persona.py:60-66 already specifies this contract; this constant + the
# `_builder_envelope_only_completion` helper enforce it in code.
_BUILDER_REQUIRED_TOOLS: frozenset[str] = frozenset(
    {"propose_pr", "update_pr", "linear_create_issue"}
)


def _extract_tool_call_names(result: dict) -> list[str]:
    """Pull tool-call function names out of a dispatcher result dict.

    Handles both shapes the dispatcher / OpenAI tool-calling protocol can
    emit:

      * `[{"name": "propose_pr", "arguments": "..."}]` — flat shape used by
        `dispatch.call_with_tools`'s `tool_call_log`.
      * `[{"function": {"name": "propose_pr", "arguments": "..."}}]` — raw
        OpenAI tool_calls passthrough.

    Returns an empty list for missing / malformed input. Never raises.
    """
    raw = result.get("tool_calls") or []
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for tc in raw:
        if not isinstance(tc, dict):
            continue
        name = tc.get("name")
        if not name:
            fn = tc.get("function")
            if isinstance(fn, dict):
                name = fn.get("name")
        if name:
            names.append(name)
    return names


def _builder_envelope_only_completion(
    *,
    persona_name: str,
    task_title: str,
    result: dict,
) -> tuple[bool, list[str]]:
    """Decide whether `result` is a builder envelope-only completion.

    Returns ``(is_envelope_only, observed_tool_names)``. ``is_envelope_only``
    is ``True`` only when the dispatch is a builder run (persona in
    ``_BUILDER_PERSONAS`` AND ``[tag:code]`` in title) AND no tool from
    ``_BUILDER_REQUIRED_TOOLS`` appears in the observed tool calls.

    Pure / synchronous so the poll-loop gate can call it once and the test
    suite can assert behaviour without booting an asyncio event loop or
    mocking soul-svc.
    """
    if persona_name not in _BUILDER_PERSONAS:
        return False, []
    if "[tag:code]" not in (task_title or ""):
        return False, []
    observed = _extract_tool_call_names(result)
    if _BUILDER_REQUIRED_TOOLS.intersection(observed):
        return False, observed
    return True, observed


def _peek_linear_ticket(task: dict) -> Optional[str]:
    """Best-effort extraction of a Linear ticket id (e.g. "SAL-2698") from
    the mesh task title or description. Returns None if no match.

    Used by the AB-21 dispatch path to stamp `X-Linear-Ticket` on the gateway
    call so traces can be joined back to Linear. Never raises — a missing
    ticket id must not block dispatch.
    """
    title = task.get("title") or ""
    m = _LINEAR_TICKET_RE.search(title)
    if m:
        return m.group(1)
    desc = task.get("description") or ""
    if not desc:
        return None
    m = _LINEAR_TICKET_RE.search(desc)
    if m:
        return m.group(1)
    return None


def _should_skip_for_human_or_terminal(
    status: Optional[dict],
) -> tuple[bool, Optional[str]]:
    """SAL-3038 / SAL-3070 predicate: should a freshly-claimed mesh task
    be skipped because its Linear ticket is human-owned or already
    terminal?

    Args:
        status: Output of ``tools.linear_get_issue_status`` —
            ``{"identifier": str, "labels": list[str], "state": str}`` —
            or ``None`` when the lookup couldn't run (no API key,
            transport error, ticket not found).

    Returns:
        ``(should_skip, reason)``. ``should_skip`` is True iff the
        ticket carries the ``human-assigned`` label OR its workflow
        state is one of ``Done`` / ``Cancelled`` / ``Duplicate``.
        ``reason`` is a short tag for the mesh-complete result body
        (``"human_assigned"`` or ``"terminal_state:<state>"``). When
        ``status`` is ``None`` we fail-open: ``(False, None)``. The
        caller is responsible for proceeding to dispatch in that case.

    Pure function — no I/O, no logging — so the orchestrator's hydrated
    ``Ticket`` path can call it too with a cheap dict adapter and the
    same predicate is the single source of truth across both dispatch
    paths. Tests in ``tests/test_main_human_assigned_gate.py``.
    """
    if not isinstance(status, dict):
        return False, None
    labels = status.get("labels") or []
    if any(
        isinstance(lbl, str) and lbl.lower() == HUMAN_ASSIGNED_LABEL
        for lbl in labels
    ):
        return True, "human_assigned"
    state = status.get("state")
    if isinstance(state, str) and state.lower() in LINEAR_TERMINAL_STATES:
        return True, f"terminal_state:{state}"
    return False, None


def _resolve_handler(handler_name: str):
    """Resolve a handler class by name across the registered handler modules.

    Returns the class object or raises ImportError/AttributeError if no
    module in `_HANDLER_MODULES` defines an attribute matching `handler_name`.
    Kept sync + small so the spawn path stays trivial to test.
    """
    last_err: Exception | None = None
    for mod_name in _HANDLER_MODULES:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError as e:
            last_err = e
            continue
        cls = getattr(mod, handler_name, None)
        if cls is not None:
            return cls
        last_err = AttributeError(
            f"module {mod_name!r} has no attribute {handler_name!r}"
        )
    if last_err is None:
        last_err = ImportError(
            f"no handler module available for {handler_name!r}"
        )
    raise last_err


async def _spawn_long_running_handler(
    task: dict,
    persona: Persona,
    mesh: MeshClient,
    soul: SoulClient,
    dispatcher: Dispatcher,
    settings,
) -> bool:
    """Attempt to spawn `persona.handler` as a detached asyncio.Task.

    Returns True if the task was spawned (and stashed in
    `_running_orchestrators`), False if the handler could not be resolved
    (in which case the mesh task is marked failed here and the caller should
    skip the one-shot dispatch path).
    """
    handler_name = persona.handler or ""
    task_id = task["id"]
    try:
        cls = _resolve_handler(handler_name)
    except (ImportError, AttributeError) as e:
        logger.warning(
            "handler %s not yet implemented for task %s: %s",
            handler_name, task_id, e,
        )
        try:
            await mesh.complete(
                task_id,
                session_id=settings.soul_session_id,
                status="failed",
                result={
                    "error": (
                        f"handler {handler_name} not yet implemented; "
                        f"see AB-04"
                    ),
                },
            )
        except Exception:
            logger.exception(
                "failed to mark task %s failed after handler resolution error",
                task_id,
            )
        return False

    try:
        orch = cls(
            task=task,
            persona=persona,
            mesh=mesh,
            soul=soul,
            dispatcher=dispatcher,
            settings=settings,
        )
    except Exception:
        logger.exception(
            "handler %s instantiation failed for task %s",
            handler_name, task_id,
        )
        try:
            await mesh.complete(
                task_id,
                session_id=settings.soul_session_id,
                status="failed",
                result={
                    "error": (
                        f"handler {handler_name} instantiation raised; "
                        f"see logs"
                    ),
                },
            )
        except Exception:
            pass
        return False

    # AB-09 Layer-2 zombie guard: if we already have a running orchestrator
    # for this linear_project_id, reject the new kickoff instead of racing
    # a second one. Layer 1 (server-side claim refresh) is deferred to AB-11
    # — until that lands, this in-process check is the safety net.
    project_id = _peek_kickoff_project_id(task)
    if project_id:
        existing_task_id = _orchestrators_by_project.get(project_id)
        if existing_task_id:
            existing_task = _running_orchestrators.get(existing_task_id)
            if existing_task is not None and not existing_task.done():
                logger.warning(
                    "rejected duplicate kickoff for project=%s "
                    "(existing task=%s)",
                    project_id,
                    existing_task_id,
                )
                try:
                    await mesh.complete(
                        task_id,
                        session_id=settings.soul_session_id,
                        status="failed",
                        result={
                            "error": (
                                f"duplicate_kickoff: existing orchestrator "
                                f"task={existing_task_id} running for "
                                f"project={project_id}"
                            ),
                        },
                    )
                except Exception:
                    logger.exception(
                        "failed to mark duplicate-kickoff task %s failed",
                        task_id,
                    )
                return False
        # No live orchestrator for this project — claim the slot before we
        # create the asyncio.Task so a second concurrent spawn in the same
        # event-loop tick can't squeeze past.
        _orchestrators_by_project[project_id] = task_id

    orch_task = asyncio.create_task(
        orch.run(), name=f"orchestrator-{handler_name}-{task_id}"
    )
    _running_orchestrators[task_id] = orch_task
    # Stream B (2026-04-29): register the live orchestrator instance so the
    # cockpit `/v1/cockpit/state` rollup can read its wave + ticket counts
    # without poking through asyncio.Task internals. Pruned on done below.
    cockpit_router.register_orchestrator(task_id, orch)
    if project_id:
        # Clear the project slot when the orchestrator task finishes (any
        # terminal state: success, exception, or cancellation). Scoped to
        # the specific project_id + task_id captured at spawn time so a
        # later spawn for the same project won't be clobbered.
        def _clear_project_slot(
            _t: asyncio.Task,
            pid: str = project_id,
            tid: str = task_id,
        ) -> None:
            if _orchestrators_by_project.get(pid) == tid:
                _orchestrators_by_project.pop(pid, None)

        orch_task.add_done_callback(_clear_project_slot)

    # AB-17-n: belt-and-suspenders — if orchestrator.run() raises a
    # BaseException that escapes its own `except Exception` guard (e.g.
    # SystemExit, or an exception inside the final `mesh.complete` call
    # itself), the mesh task would never be /completed and the kickoff
    # would look "silently dead" forever. Inspect the task's exception in
    # a done-callback and fire a best-effort failed-complete so the mesh
    # reconciles. Paired with the _dispatch_wave / _wait_for_wave_gate
    # deadlock detectors above.
    def _orch_done(
        t: asyncio.Task,
        tid: str = task_id,
        sid: str = settings.soul_session_id,
    ) -> None:
        # Stream B: prune the cockpit registry on terminal state regardless
        # of success/failure so the rollup doesn't keep reporting a finished
        # orchestrator as "active".
        cockpit_router.deregister_orchestrator(tid)
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            exc = RuntimeError("orchestrator cancelled")
        except Exception:
            # .exception() can raise InvalidStateError if called too
            # early; defensive guard — nothing actionable here.
            return
        if exc is None:
            return
        logger.error(
            "orchestrator task %s died unexpectedly: %r", tid, exc
        )
        err_body = f"handler_died: {type(exc).__name__}: {exc}"
        try:
            asyncio.create_task(
                mesh.complete(
                    tid,
                    session_id=sid,
                    status="failed",
                    result={"error": err_body},
                )
            )
        except RuntimeError:
            # No running loop (shouldn't happen in this callback, but be
            # safe — done-callbacks fire on the loop that owned the task).
            logger.exception(
                "could not schedule failed-complete for task %s", tid
            )

    orch_task.add_done_callback(_orch_done)
    logger.info(
        "spawned long-running handler",
        extra={
            "task_id": task_id,
            "handler": handler_name,
            "persona": persona.name,
            "linear_project_id": project_id,
        },
    )
    return True


async def _run_health_server(app, port: int) -> None:
    cfg = uvicorn.Config(app, host="0.0.0.0", port=port,
                         log_level="warning", access_log=False)
    server = uvicorn.Server(cfg)
    await server.serve()


def _fallback_urls(settings) -> list[str] | None:
    urls = list(settings.soul_api_urls or [])
    primary = settings.soul_api_url.rstrip("/")
    extras = [u.rstrip("/") for u in urls if u.rstrip("/") != primary]
    return extras or None


async def main() -> None:
    settings = config.get_settings()
    log.setup_logging(settings.log_level, settings.log_format)
    logger.info("alfred-coo v0 starting",
                extra={"session_id": settings.soul_session_id,
                       "node_id": settings.soul_node_id})
    # SAL-2905: emit one INFO line summarising the configured GitHub
    # identities (or single-token-mode warning) so operators can
    # verify split-identity is in effect from the daemon log alone.
    log_identity_summary()

    fallback = _fallback_urls(settings)

    mesh = MeshClient(
        base_url=settings.soul_api_url,
        api_key=settings.soul_api_key,
        fallback_urls=fallback,
    )
    soul = SoulClient(
        base_url=settings.soul_api_url,
        api_key=settings.soul_api_key,
        session_id=settings.soul_session_id,
        fallback_urls=fallback,
    )
    dispatcher = Dispatcher(
        ollama_url=settings.ollama_url,
        anthropic_key=settings.anthropic_api_key,
        openrouter_key=settings.openrouter_api_key,
        gateway_url=settings.gateway_url,
        autobuild_soulkey=settings.autobuild_soulkey,
        tiresias_tenant=settings.tiresias_tenant,
    )

    # Health endpoint in a background task.
    app = health.make_app()
    # Stream B (2026-04-29): mount the cockpit rollup endpoint on the same
    # FastAPI app so alfred-portal can fetch `/v1/cockpit/state` from the
    # daemon's existing port without standing up a second uvicorn server.
    cockpit_router.attach_cockpit(
        app,
        soul_api_url=settings.soul_api_url,
        soul_api_key=settings.soul_api_key,
    )
    asyncio.create_task(_run_health_server(app, settings.health_port))
    health.mark_alive()

    # Initial heartbeat (non-fatal if it fails).
    try:
        await mesh.heartbeat(
            session_id=settings.soul_session_id,
            node_id=settings.soul_node_id,
            harness=settings.soul_harness,
            current_task="v0 daemon boot",
        )
        logger.info("initial heartbeat ok")
    except Exception as e:
        logger.warning("initial heartbeat failed (continuing): %s", e)

    while True:
        try:
            pending = await mesh.list_pending(limit=10)
            claimed = []
            for task in pending:
                title = task.get("title", "") or ""
                persona_name = parse_persona_tag(title)
                is_unified = "[unified-plan-wave-1]" in title
                if not (persona_name or is_unified):
                    continue

                # SAL-2952: pre-claim self-orchestrator guard. If this
                # task id is already a running long-running orchestrator
                # in THIS process, skip the claim entirely. Without this,
                # an expired soul-svc claim lease lets the parent kickoff
                # re-surface as `pending`; the loop re-claims it; the
                # AB-09 Layer-2 zombie guard in _spawn_long_running_handler
                # marks the re-claimed task failed with `duplicate_kickoff:`,
                # which the PR #92 cancel-handler filter ignores — but the
                # round-trip still burns a main-loop tick and a mesh.claim
                # call that should have gone to dispatcher work. Evidence:
                # 57 self-inflicted events at 50 s cadence in v7y wave-1.
                if _is_already_running_orchestrator(task["id"]):
                    logger.warning(
                        "[claim] skipping pre-claim of own running "
                        "orchestrator task %s; soul-svc claim lease likely "
                        "expired but orchestrator is still alive in-process. "
                        "SAL-2952.",
                        task["id"],
                    )
                    continue

                # Try to claim. Claim returns the updated task record, or raises.
                try:
                    await mesh.claim(
                        task["id"], settings.soul_session_id, settings.soul_node_id
                    )
                except Exception as e:
                    logger.debug("claim skipped for %s (likely already claimed): %s",
                                 task.get("id"), e)
                    continue

                claimed.append(task["id"])
                persona = get_persona(persona_name or "default")
                logger.info("claimed task",
                            extra={"task_id": task.get("id"),
                                   "persona": persona.name,
                                   "title": title[:120]})

                # Long-running handler fork. If the persona declares a
                # handler class, spawn it as a detached asyncio.Task and
                # continue polling — main loop keeps servicing other
                # personas in parallel. Heartbeat inside the orchestrator
                # keeps this task's liveness fresh. See plan F §1.
                if persona.handler:
                    await _spawn_long_running_handler(
                        task=task,
                        persona=persona,
                        mesh=mesh,
                        soul=soul,
                        dispatcher=dispatcher,
                        settings=settings,
                    )
                    continue

                # SAL-3038 / SAL-3070 (2026-04-28): human-assigned + terminal-state
                # gate on the bare claim path. PR #171 closed this gap inside the
                # orchestrator's wave-dispatch loop (orchestrator.py:3076-3084),
                # but tasks claimed *directly* from the mesh poll here never went
                # through hydration so the label was silently ignored. Result: 47
                # mesh tasks queued for SAL-3038 at 00:40-00:54 UTC kept being
                # consumed after the label was applied later, producing 22 zombie
                # PRs at ~6 min/PR. Same pattern was brewing on SAL-3070. We do
                # one Linear GET per claim (50s tick interval keeps cost trivial)
                # and fail-open on any lookup failure — better to dispatch one
                # extra builder than stall the loop on Linear flakiness.
                ticket_code = _peek_linear_ticket(task)
                if ticket_code is None:
                    logger.debug(
                        "[gate] no linear ticket id in task %s title; gate not applicable",
                        task.get("id"),
                    )
                else:
                    try:
                        from .tools import linear_get_issue_status
                        ticket_status = await linear_get_issue_status(ticket_code)
                    except Exception as e:
                        logger.warning(
                            "[gate] linear_get_issue_status raised for %s; "
                            "fail-open and proceeding to dispatch: %s",
                            ticket_code, e,
                        )
                        ticket_status = None
                    should_skip, skip_reason = _should_skip_for_human_or_terminal(
                        ticket_status
                    )
                    if should_skip:
                        logger.info(
                            "[gate] skipping dispatch of %s (mesh task %s): %s "
                            "labels=%s state=%s",
                            ticket_code,
                            task.get("id"),
                            skip_reason,
                            (ticket_status or {}).get("labels"),
                            (ticket_status or {}).get("state"),
                        )
                        try:
                            await mesh.complete(
                                task["id"],
                                session_id=settings.soul_session_id,
                                status="failed",
                                result={
                                    "error": "human_assigned_or_terminal",
                                    "reason": skip_reason,
                                    "linear_ticket": ticket_code,
                                    "linear_state": (ticket_status or {}).get("state"),
                                    "linear_labels": (ticket_status or {}).get("labels"),
                                },
                            )
                        except Exception:
                            logger.exception(
                                "[gate] failed to mark mesh task %s failed after gate hit; "
                                "task will likely re-surface and re-trigger the gate",
                                task.get("id"),
                            )
                        continue

                # Context load (non-fatal on error). Scoped by persona topics.
                try:
                    recent = await soul.recent_memories(
                        limit=20,
                        topics=persona.topics or None,
                    )
                except Exception as e:
                    logger.warning("recent_memories failed, continuing with empty context: %s", e)
                    recent = []

                if isinstance(recent, dict):
                    recent = recent.get("memories", [])
                context_str = "\n".join(
                    (m.get("content", "") or "")[:500]
                    for m in (recent or [])[:10]
                    if isinstance(m, dict)
                )
                system_prompt = (
                    persona.system_prompt
                    + "\n\nRECENT CONTEXT:\n"
                    + context_str
                    + OUTPUT_CONTRACT
                )

                model = dispatch.select_model(task, persona)
                tool_specs = resolve_tools(persona.tools or [])
                # AB-21: stamp persona + linear ticket + mesh task id on every
                # LLM call so the gateway trace middleware can correlate.
                dispatch_ctx = DispatchContext(
                    persona=persona.name,
                    linear_ticket=_peek_linear_ticket(task),
                    mesh_task_id=str(task.get("id")) if task.get("id") else None,
                )
                # AB-17-m: size-gated iteration cap for builder personas.
                # Reviewers get None here and fall back to MAX_TOOL_ITERATIONS
                # inside the dispatcher (short review loops don't need 20
                # turns). Computed once so the log + the call agree.
                iteration_cap = _builder_iteration_cap(persona.name, task)
                size_label = _peek_size_label(task)
                # SAL-2978: surface fix-round flag in dispatch log so ops can
                # tell at a glance whether the bumped cap kicked in.
                is_fix_round = _is_fix_round_dispatch(task)
                logger.info("dispatching",
                            extra={"task_id": task.get("id"),
                                   "model": model,
                                   "persona": persona.name,
                                   "linear_ticket": dispatch_ctx.linear_ticket,
                                   "tools_enabled": len(tool_specs) > 0,
                                   "tool_count": len(tool_specs),
                                   "iteration_cap": iteration_cap,
                                   "size_label": size_label,
                                   "is_fix_round": is_fix_round,
                                   "iteration_count_reset": True})
                if iteration_cap is not None:
                    # AB-17-m: separate human-readable log so ops can grep for
                    # per-ticket caps without digging through structured extras.
                    # SAL-2978: include fix-round flag so the log line at this
                    # level is self-describing.
                    logger.info(
                        "dispatching with iteration_cap=%d size=%s "
                        "fix_round=%s iteration_count_reset=True",
                        iteration_cap, size_label or "unknown",
                        is_fix_round,
                    )

                try:
                    if tool_specs:
                        ctx_token = set_current_task_id(task["id"])
                        # SAL-2905: stamp the active persona so
                        # GitHub-touching tool handlers can route to
                        # the right identity-class token.
                        persona_token = set_current_persona(persona.name)
                        try:
                            result = await dispatcher.call_with_tools(
                                model,
                                system_prompt,
                                task.get("description") or task.get("title", "") or "(no content)",
                                tools=tool_specs,
                                fallback_model=persona.fallback_model,
                                context=dispatch_ctx,
                                max_iterations=iteration_cap,
                            )
                        finally:
                            reset_current_persona(persona_token)
                            reset_current_task_id(ctx_token)
                    else:
                        result = await dispatcher.call(
                            model,
                            system_prompt,
                            task.get("description") or task.get("title", "") or "(no content)",
                            fallback_model=persona.fallback_model,
                            context=dispatch_ctx,
                        )
                except Exception as e:
                    logger.exception("dispatch failed for task %s", task.get("id"))
                    # Best-effort mark the task as failed so it doesn't sit claimed forever.
                    try:
                        await mesh.complete(
                            task["id"],
                            session_id=settings.soul_session_id,
                            status="failed",
                            result={
                                "error": f"dispatch failure: {type(e).__name__}: {str(e)[:500]}"
                            },
                        )
                    except Exception:
                        pass
                    continue

                raw_content = result.get("content", "") or ""
                envelope = parse_envelope(raw_content)
                artifact_paths: list[str] = []
                if envelope is not None:
                    try:
                        artifact_paths = write_artifacts(
                            task_id=task["id"],
                            artifacts=envelope.artifacts,
                        )
                    except Exception:
                        logger.exception("artifact write pass failed for task %s",
                                         task.get("id"))

                # SAL-3140 (2026-04-28): Enforce builder contract — silent-completion
                # bug where models emit envelope summaries claiming completion without
                # actually calling propose_pr/update_pr/linear_create_issue. Without
                # this gate, the orchestrator marks the task complete based on the
                # envelope summary, no PR lands, ticket re-queues into the same loop.
                # Pattern documented at persona.py:60-66 but not enforced until now.
                _gate_failed, _observed = _builder_envelope_only_completion(
                    persona_name=persona.name,
                    task_title=task.get("title") or "",
                    result=result,
                )
                if _gate_failed:
                    _linear_ticket = _peek_linear_ticket(task)
                    logger.warning(
                        "builder rejected envelope-only completion task_id=%s "
                        "linear_ticket=%s tools_observed=%s",
                        task.get("id"),
                        _linear_ticket,
                        _observed,
                    )
                    try:
                        await mesh.complete(
                            task["id"],
                            session_id=settings.soul_session_id,
                            status="failed",
                            result={
                                "error": (
                                    "builder envelope-only completion: "
                                    "propose_pr/update_pr/linear_create_issue "
                                    "not invoked"
                                ),
                                "tool_calls_observed": _observed,
                                "persona": persona.name,
                                "linear_ticket": _linear_ticket,
                            },
                        )
                    except Exception:
                        logger.exception(
                            "builder gate: mesh.complete(failed) raised for task %s",
                            task.get("id"),
                        )
                    continue

                try:
                    complete_result: dict = {
                        "model": result.get("model_used"),
                        "tokens": {
                            "in": result.get("tokens_in"),
                            "out": result.get("tokens_out"),
                        },
                        "structured": envelope is not None,
                    }
                    if envelope is not None:
                        complete_result["summary"] = envelope.summary
                        complete_result["artifact_paths"] = artifact_paths
                        if envelope.follow_up_tasks:
                            complete_result["follow_up_tasks"] = envelope.follow_up_tasks
                    else:
                        # Unstructured fallback — persist full raw text so nothing is lost.
                        complete_result["content"] = raw_content
                    # Tool-use metadata (present when persona.tools is non-empty and model invoked tools)
                    if result.get("tool_calls"):
                        complete_result["tool_calls"] = result.get("tool_calls")
                        complete_result["tool_iterations"] = result.get("iterations")
                    await mesh.complete(
                        task["id"],
                        session_id=settings.soul_session_id,
                        result=complete_result,
                    )
                except Exception:
                    logger.exception("complete failed for task %s", task.get("id"))
                    continue

                # Soul memory summary entry.
                mem_summary = (
                    envelope.summary if envelope is not None
                    else raw_content[:500]
                )
                mem_topics = ["coo-daemon", "task-complete"] + list(persona.topics or [])
                try:
                    await soul.write_memory(
                        f"COO daemon completed task {task['id']} ({persona.name}): "
                        f"{mem_summary}"
                        + (f" | artifacts: {', '.join(artifact_paths)}" if artifact_paths else ""),
                        topics=mem_topics,
                    )
                except Exception as e:
                    logger.warning("soul write_memory failed (non-fatal): %s", e)

            health.mark_alive()

            try:
                await mesh.heartbeat(
                    session_id=settings.soul_session_id,
                    node_id=settings.soul_node_id,
                    harness=settings.soul_harness,
                    current_task=f"polled; claimed {len(claimed)}",
                )
            except Exception as e:
                logger.warning("periodic heartbeat failed: %s", e)

            await asyncio.sleep(settings.mesh_poll_interval_seconds)

        except Exception:
            logger.exception("unhandled error in main loop; continuing")
            health.mark_alive()
            await asyncio.sleep(settings.mesh_poll_interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
