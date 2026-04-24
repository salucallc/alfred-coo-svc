"""AutonomousBuildOrchestrator — core wave scheduler + dependency resolver.

Long-running asyncio task spawned by `main.py` when a kickoff task tagged
`[persona:autonomous-build-a]` is claimed (plan F §1). The orchestrator:

1. Parses the kickoff JSON payload (budget, wave_order, concurrency, ...).
2. Tries to restore state from soul memory; else fresh state.
3. Builds the Linear ticket graph via the AB-03 tools.
4. Walks waves in order. Per wave: dispatch ready tickets respecting
   `blocks_in` + per-epic cap + global parallel cap, poll children for
   completion, update ticket statuses, checkpoint state, sleep 45s.
5. On all-green across all waves: run `on_all_green` actions as child
   tasks through `alfred-coo-a`, then mark the kickoff complete.

AB-05 will fill in `_check_budget()` + Slack cadence; AB-06 fills in
`_maybe_ss08_gate()`. Those sites are called here as stubs so downstream
PRs can land without reshaping the loop.

Constructor is kwargs-only — see `main._spawn_long_running_handler`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .budget import BudgetTracker, make_tracker
from .cadence import SlackCadence
from .dry_run import maybe_apply_dry_run
from .graph import (
    TERMINAL_STATES,
    Ticket,
    TicketGraph,
    TicketStatus,
    build_ticket_graph,
)
from .state import OrchestratorState, checkpoint, restore


# Pre-compiled so `_extract_pr_url` doesn't rebuild it every child poll.
_PR_URL_RE = re.compile(r"https://github\.com/[^\s)]+/pull/\d+")


logger = logging.getLogger("alfred_coo.autonomous_build.orchestrator")


# Defaults, overridable by the kickoff payload.
DEFAULT_MAX_PARALLEL_SUBS = 6
DEFAULT_PER_EPIC_CAP = 3
DEFAULT_CLOUD_MODEL_SLOTS = 10
DEFAULT_BUDGET_USD = 30.0
DEFAULT_STATUS_CADENCE_MIN = 20
DEFAULT_POLL_SLEEP_SEC = 45

# Soft-green threshold on non-critical-path failures: if ≥90% of the wave
# is merged_green and no critical-path failures, the wave is allowed to
# close with a Slack warning. Critical-path failures always hard-halt.
SOFT_GREEN_THRESHOLD = 0.9

# Default: a critical-path ticket stuck in-flight for >30 min triggers
# a Slack stall ping. Overridable by the payload for tests / tuning.
DEFAULT_STALL_THRESHOLD_SEC = 30 * 60

# Default Slack channel for the cadence poster if the payload omits it.
DEFAULT_STATUS_CHANNEL = "C0ASAKFTR1C"  # #batcave

# AB-08: hard cap on REQUEST_CHANGES → respawn cycles. Tickets that blow the
# cap are marked FAILED; the wave gate's existing critical-path + soft-green
# logic handles the rest.
MAX_REVIEW_CYCLES = 3

# AB-08: compiled regexes for verdict extraction. Uppercase-only keywords
# keep false positives low (hawkman prompt spec: shout the verdict, not
# "we approve of this idea in prose").
_VERDICT_APPROVE_RE = re.compile(r"\bAPPROVE\b")
_VERDICT_REQUEST_CHANGES_RE = re.compile(r"\bREQUEST_CHANGES\b")

# Placeholder used when a REQUEST_CHANGES review body is empty/missing.
_NO_REVIEW_BODY_NOTE = (
    "(no review body captured; see the review task record in soul memory)"
)


# AB-13 · Target grounding ---------------------------------------------------
#
# `_child_task_body` used to tell the sub "open ONE PR to the target Saluca
# repo" without pinning owner/repo/paths. Children guessed, producing
# phantom root `docker-compose.yml` files (PR #32, SAL-2634, 2026-04-24).
#
# This table pre-resolves `{owner, repo, paths}` for every wave-0 / wave-1
# ticket in the v1-GA plan docs. The orchestrator renders a ``## Target``
# block into the child body so the sub has an exact file list to touch,
# and — per Plan H §2 G-2 + Plan H §5 R-d (child-side escalation) — an
# unmapped code emits an `(unresolved)` block telling the child to STOP
# and open a grounding-gap Linear issue instead of guessing.


@dataclass(frozen=True)
class TargetHint:
    """Pre-resolved repo + paths for a v1-GA plan-doc ticket code.

    Consumed by ``AutonomousBuildOrchestrator._child_task_body`` to emit a
    ``## Target`` block in the dispatched child task body. Fields map
    one-to-one to the block's keys so rendering is trivial.
    """

    owner: str
    repo: str
    paths: Tuple[str, ...]
    base_branch: str = "main"
    branch_hint: Optional[str] = None
    notes: Optional[str] = None


#: Keyed by plan-doc ticket code (e.g. ``OPS-01``, ``F08``, ``TIR-01``,
#: ``S-01``). Codes MUST be uppercase with the canonical separator used
#: in the plan doc (``OPS-01`` with dash, ``F08`` with no separator —
#: matching the titles the mesh will actually see).
#:
#: Source of truth: ``Z:/_planning/v1-ga/{A,C,D,E}_*.md`` on minipc, or
#: ``https://raw.githubusercontent.com/salucallc/alfred-coo-svc/main/
#: plans/v1-ga/*.md`` (added by the children fetch).
_TARGET_HINTS: Mapping[str, TargetHint] = {
    # ── Epic D · OPS layer (salucallc/alfred-coo-svc, deploy/appliance) ─
    "OPS-01": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/docker-compose.yaml",),
        branch_hint="feature/sal-2634-mc-ops-network",
        notes="add mc-ops network + 4 volumes (grafana, prometheus, loki, restic)",
    ),
    "OPS-02": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "deploy/appliance/docker-compose.yaml",
            "deploy/appliance/IMAGE_PINS.md",
        ),
        branch_hint="feature/ops-02-pin-images",
        notes="pin all image versions; grep ':latest' must return 0 matches",
    ),
    "OPS-03": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "deploy/appliance/caddy/Caddyfile",
            "deploy/appliance/docker-compose.yaml",
        ),
        branch_hint="feature/ops-03-caddy-routes",
        notes="Caddy routes /ops /auth /vault → grafana/authelia/infisical",
    ),
    # ── Epic C/F · Fleet mode endpoint (multi-repo) ─────────────────────
    "F01": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=("db/migrations/0007_fleet_endpoints.sql",),
        branch_hint="feature/f01-fleet-migration",
        notes="soul-svc migration 0007 for fleet tables (4 tables)",
    ),
    "F02": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(
            "routers/fleet.py",
            "tests/test_fleet_register.py",
        ),
        branch_hint="feature/f02-fleet-register",
        notes="/v1/fleet/register endpoint; valid token -> 201",
    ),
    "F03": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "src/mcctl/commands/token.py",
            "tests/test_mcctl_token.py",
        ),
        branch_hint="feature/f03-mcctl-token-create",
        notes="mcctl token create CLI (one-shot bootstrap tokens)",
    ),
    "F07": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "src/alfred_coo/persona_loader.py",
            "src/alfred_coo/main.py",
        ),
        branch_hint="feature/f07-coo-mode",
        notes="COO_MODE env var (hub|endpoint) + persona_loader.py",
    ),
    "F08": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(
            "soul_lite/__init__.py",
            "soul_lite/service.py",
            "soul_lite/Dockerfile",
            "tests/test_soul_lite.py",
        ),
        branch_hint="feature/f08-soul-lite",
        notes="new soul-lite service: sqlite + /v1/memory/* API for endpoints",
    ),
    # ── Epic E · soul-svc gap closure (salucallc/soul-svc prod variant) ─
    "S-01": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(
            "routers/memory.py",
            "tests/test_bulk_import_topics_queryable.py",
        ),
        branch_hint="feature/s01-index-topics-on-import",
        notes="fix: /v1/memory/import must index TKHR topics",
    ),
    "S-02": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(
            "routers/memory.py",
            "tests/test_memory_dup_409.py",
        ),
        branch_hint="feature/s02-dup-409",
        notes="fix: duplicate content_hash returns 409 not 500",
    ),
    "S-04": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(
            "routers/metrics.py",
            "main.py",
            "tests/test_metrics_endpoint.py",
        ),
        branch_hint="feature/s04-metrics",
        notes="new /metrics endpoint with prometheus counters + histograms",
    ),
    "S-09": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(
            "db/pool.py",
            "db/repository.py",
            "routers/memory.py",
            "tests/test_asyncpg_pool_init.py",
        ),
        branch_hint="feature/s09-asyncpg-repository",
        notes="introduce asyncpg repository layer; swap Supabase SDK in routers/memory.py",
    ),
    # ── Epic A · Tiresias in appliance ──────────────────────────────────
    "TIR-01": TargetHint(
        owner="salucallc",
        repo="tiresias-sovereign",
        paths=(
            "README.md",
            "pyproject.toml",
            "src/tiresias_sovereign/__init__.py",
            ".github/workflows/ci.yml",
        ),
        branch_hint="feature/tir-01-scaffold",
        notes="scaffold new salucallc/tiresias-sovereign repo + CI",
    ),
    "TIR-02": TargetHint(
        owner="salucallc",
        repo="tiresias-sovereign",
        paths=(
            "src/tiresias_sovereign/principles/registry.json",
            "src/tiresias_sovereign/principles/loader.py",
            "tests/test_principle_registry.py",
        ),
        branch_hint="feature/tir-02-principle-registry",
        notes="embed principle_registry.json + hash-chain loader",
    ),
    "TIR-07": TargetHint(
        owner="salucallc",
        repo="tiresias-sovereign",
        paths=(
            "db/migrations/0001_tiresias_audit.sql",
            "tests/test_audit_schema.py",
        ),
        branch_hint="feature/tir-07-audit-migration",
        notes="DB migrations for tiresias_audit schema",
    ),
    "TIR-08": TargetHint(
        owner="salucallc",
        repo="tiresias-sovereign",
        paths=(
            "src/tiresias_sovereign/mcp_llm/router.py",
            "tests/test_mcp_llm_cascade.py",
        ),
        branch_hint="feature/tir-08-mcp-llm-cascade",
        notes="mcp-llm cascade router (principle-aware routing)",
    ),
}


def _render_target_block(code: str) -> str:
    """Render a ``## Target`` markdown block for the given plan-doc code.

    If ``code`` is not in ``_TARGET_HINTS`` (or is empty), returns an
    ``(unresolved)`` block so the child knows to escalate at Step 0 of its
    persona grounding protocol (Plan H §2 G-1) rather than guessing.
    """
    hint = _TARGET_HINTS.get((code or "").upper())
    if hint is None:
        return (
            "## Target\n"
            "(unresolved — consult plan doc; STOP and escalate via "
            "linear_create_issue per Step 0 of your persona protocol)\n"
        )
    paths_block = "\n".join(f"  - {p}" for p in hint.paths)
    lines = [
        "## Target",
        f"owner: {hint.owner}",
        f"repo:  {hint.repo}",
        "paths:",
        paths_block,
        f"base_branch: {hint.base_branch}",
    ]
    if hint.branch_hint:
        lines.append(f"branch_hint: {hint.branch_hint}")
    if hint.notes:
        lines.append(f"notes: {hint.notes}")
    return "\n".join(lines) + "\n"


class AutonomousBuildOrchestrator:
    """See module docstring."""

    # ── construction ────────────────────────────────────────────────────────

    def __init__(
        self,
        *,
        task: Dict[str, Any],
        persona,
        mesh,
        soul,
        dispatcher,
        settings,
    ) -> None:
        self.task = task
        self.task_id: str = task["id"]
        self.persona = persona
        self.mesh = mesh
        self.soul = soul
        self.dispatcher = dispatcher
        self.settings = settings

        # Parsed kickoff payload (populated in run()).
        self.payload: Dict[str, Any] = {}
        # Graph + state are populated after parse + restore.
        self.graph: TicketGraph = TicketGraph()
        self.state: OrchestratorState = OrchestratorState(kickoff_task_id=self.task_id)

        # Tunables (overridden by payload during run()).
        self.max_parallel_subs: int = DEFAULT_MAX_PARALLEL_SUBS
        self.per_epic_cap: int = DEFAULT_PER_EPIC_CAP
        self.budget_usd: float = DEFAULT_BUDGET_USD
        self.status_cadence_min: int = DEFAULT_STATUS_CADENCE_MIN
        self.poll_sleep_sec: int = DEFAULT_POLL_SLEEP_SEC
        self.wave_order: List[int] = [0, 1, 2, 3]
        self.linear_project_id: str = ""

        # Stash the last time we posted a cadence tick so _status_tick can
        # rate-limit itself without a separate timer.
        self._last_cadence_ts: float = 0.0

        # Injectable tool fetchers — tests swap these without monkeypatching.
        # Defaults resolve lazily so import of this module never depends on
        # LINEAR_API_KEY being set (e.g. in unit tests that stub out the
        # whole graph path).
        self._list_project_issues = None
        self._get_issue_relations = None

        # AB-05: budget tracking + Slack cadence + stall watcher.
        # Constructed with defaults here; `_parse_payload` replaces them
        # with payload-configured instances once the kickoff JSON is parsed.
        self.budget_tracker: BudgetTracker = BudgetTracker(max_usd=self.budget_usd)
        self.cadence: SlackCadence = SlackCadence(
            channel=DEFAULT_STATUS_CHANNEL,
            interval_minutes=self.status_cadence_min,
        )
        self._drain_mode: bool = False
        # Map ticket UUID -> UNIX ts of the last orchestrator-observed
        # status transition. Used by `_stall_watcher` to decide whether a
        # critical-path ticket has been stuck too long.
        self._ticket_transition_ts: Dict[str, float] = {}
        # Tracks which CP stall warnings have already fired so a single
        # stall event doesn't post on every dispatch loop iteration.
        self._stall_pinged: Dict[str, float] = {}
        # Last batch of completed mesh-task records from `_poll_children`;
        # read by `_check_budget` to tally token spend without re-querying
        # the mesh.
        self._last_completed_records: List[Dict[str, Any]] = []
        # AB-08: same batch indexed by mesh task id so `_poll_reviews` can
        # look up review task records without a second `list_tasks` round
        # trip. Populated by `_poll_children` on every tick.
        self._last_completed_by_id: Dict[str, Dict[str, Any]] = {}
        # Overridable via payload (for tests that want a shorter threshold).
        self.stall_threshold_sec: int = DEFAULT_STALL_THRESHOLD_SEC

        # AB-07: if AUTONOMOUS_BUILD_DRY_RUN is set, swap mesh/slack/linear
        # clients for the in-process DryRunAdapter. The returned adapter (if
        # any) is stashed on the instance as `self._dry_run_adapter` by
        # `apply_dry_run` so tests + operators can inspect it.
        self._dry_run_adapter = maybe_apply_dry_run(self)

    # ── public entry point ─────────────────────────────────────────────────

    async def run(self) -> None:
        """Top-level lifecycle. Broad try/except so orchestrator crashes
        always mark the kickoff task failed + stash state in the result."""
        logger.info("autonomous_build orchestrator starting (task=%s)", self.task_id)
        try:
            await self._run_inner()
        except Exception as e:  # noqa: BLE001 — top-level sink is intentional
            logger.exception("autonomous_build orchestrator crashed")
            await self._fail_kickoff(
                reason=f"orchestrator crashed: {type(e).__name__}: {str(e)[:500]}",
            )

    async def _run_inner(self) -> None:
        # 1. Parse payload.
        self._parse_payload()

        # 2. Restore state if a prior checkpoint exists.
        restored = await restore(self.soul, self.task_id)
        if restored is not None:
            self.state = restored
            logger.info(
                "resumed orchestrator state from soul memory "
                "(wave=%d, spend=$%.2f, tickets_tracked=%d)",
                self.state.current_wave,
                self.state.cumulative_spend_usd,
                len(self.state.ticket_status),
            )
        else:
            self.state = OrchestratorState(kickoff_task_id=self.task_id)
            self.state.record_event("orchestrator_started", task_id=self.task_id)

        # 3. Build ticket graph.
        self.graph = await self._build_graph()

        # Merge restored status back into the fresh graph so we don't
        # re-dispatch tickets we already closed last run.
        self._apply_restored_status()

        # 4. Main wave loop.
        for wave in self.wave_order:
            self.state.current_wave = wave
            logger.info("entering wave %d", wave)
            self.state.record_event("wave_enter", wave=wave)
            await self._dispatch_wave(wave)
            await self._wait_for_wave_gate(wave)
            self.state.record_event("wave_exit", wave=wave)
            await checkpoint(self.state, self.soul, self.task_id)

        # 5. on_all_green.
        await self._run_on_all_green_actions()

        # 6. Mark kickoff complete.
        await self._complete_kickoff()

    # ── payload parsing ─────────────────────────────────────────────────────

    def _parse_payload(self) -> None:
        """Parse the kickoff task description as JSON. Unknown keys are
        logged-and-continued (forward compat per plan F §2)."""
        desc = self.task.get("description") or ""
        try:
            payload = json.loads(desc) if desc else {}
        except json.JSONDecodeError:
            logger.warning(
                "kickoff description is not JSON; continuing with defaults"
            )
            payload = {}
        if not isinstance(payload, dict):
            logger.warning(
                "kickoff payload not a JSON object (%s); ignoring",
                type(payload).__name__,
            )
            payload = {}
        self.payload = payload

        # Linear project.
        self.linear_project_id = str(
            payload.get("linear_project_id") or ""
        ).strip()
        if not self.linear_project_id:
            raise RuntimeError(
                "kickoff payload missing linear_project_id — cannot build ticket graph"
            )

        # Concurrency.
        concurrency = payload.get("concurrency") or {}
        self.max_parallel_subs = int(
            concurrency.get("max_parallel_subs") or DEFAULT_MAX_PARALLEL_SUBS
        )
        self.per_epic_cap = int(
            concurrency.get("per_epic_cap") or DEFAULT_PER_EPIC_CAP
        )

        # Budget.
        budget = payload.get("budget") or {}
        try:
            self.budget_usd = float(budget.get("max_usd") or DEFAULT_BUDGET_USD)
        except (TypeError, ValueError):
            self.budget_usd = DEFAULT_BUDGET_USD

        # Cadence.
        status_cadence = payload.get("status_cadence") or {}
        try:
            self.status_cadence_min = int(
                status_cadence.get("interval_minutes") or DEFAULT_STATUS_CADENCE_MIN
            )
        except (TypeError, ValueError):
            self.status_cadence_min = DEFAULT_STATUS_CADENCE_MIN
        slack_channel = str(
            status_cadence.get("slack_channel")
            or payload.get("slack_channel")
            or DEFAULT_STATUS_CHANNEL
        ).strip() or DEFAULT_STATUS_CHANNEL

        # Stall threshold (optional).
        stall_override = status_cadence.get("stall_threshold_sec")
        if stall_override is not None:
            try:
                self.stall_threshold_sec = int(stall_override)
            except (TypeError, ValueError):
                self.stall_threshold_sec = DEFAULT_STALL_THRESHOLD_SEC

        # Wave order.
        wave_order = payload.get("wave_order")
        if isinstance(wave_order, list) and wave_order:
            self.wave_order = [int(w) for w in wave_order if isinstance(w, (int, str))]

        # AB-05: build the payload-configured tracker + cadence. Keep the
        # previously-constructed defaults if the payload omits a field so
        # tests that hand-roll an orchestrator still get usable instances.
        self.budget_tracker = make_tracker(payload.get("budget"))
        self.cadence = SlackCadence(
            channel=slack_channel,
            interval_minutes=self.status_cadence_min,
        )

        # AB-07: dry-run mode rebuilds slack wiring on cadence reconstruction.
        # Re-bind the adapter's slack_post fn so the new cadence points at
        # the in-process stub instead of the BUILTIN_TOOLS resolver.
        if self._dry_run_adapter is not None:
            self.cadence._slack_post_fn = self._dry_run_adapter.slack_post

        logger.info(
            "parsed kickoff payload: project=%s budget=$%.2f "
            "max_parallel_subs=%d per_epic_cap=%d waves=%s "
            "cadence=%dmin channel=%s",
            self.linear_project_id,
            self.budget_usd,
            self.max_parallel_subs,
            self.per_epic_cap,
            self.wave_order,
            self.status_cadence_min,
            slack_channel,
        )

    # ── graph build ─────────────────────────────────────────────────────────

    async def _build_graph(self) -> TicketGraph:
        """Resolve the AB-03 Linear tools + run the graph builder.

        Tools live in `alfred_coo.tools.BUILTIN_TOOLS` — we use the handlers
        directly rather than going through the model's tool-call path, since
        we're the orchestrator, not a model.
        """
        if self._list_project_issues is None or self._get_issue_relations is None:
            # Lazy import to avoid paying the tools.py import cost (and its
            # env-var checks) until actually needed. Tests that inject
            # fetchers never hit this branch.
            from alfred_coo.tools import BUILTIN_TOOLS

            list_spec = BUILTIN_TOOLS.get("linear_list_project_issues")
            rel_spec = BUILTIN_TOOLS.get("linear_get_issue_relations")
            if list_spec is None or rel_spec is None:
                raise RuntimeError(
                    "AB-03 tools missing from BUILTIN_TOOLS — check "
                    "tools.py registration"
                )
            self._list_project_issues = list_spec.handler
            self._get_issue_relations = rel_spec.handler

        return await build_ticket_graph(
            project_id=self.linear_project_id,
            list_project_issues=self._list_project_issues,
            # Backfill is opt-in inside build_ticket_graph; we pass the
            # fetcher so the builder can use it when needed.
            get_issue_relations=self._get_issue_relations,
        )

    def _apply_restored_status(self) -> None:
        """Merge prior-run statuses stored in `self.state.ticket_status` onto
        the fresh graph nodes so we don't re-dispatch tickets we already
        closed before the restart."""
        for uuid, status_str in (self.state.ticket_status or {}).items():
            node = self.graph.nodes.get(uuid)
            if node is None:
                continue
            try:
                node.status = TicketStatus(status_str)
            except ValueError:
                logger.warning(
                    "unknown ticket status %r in restored state; keeping %s",
                    status_str, node.status,
                )
            child_id = (self.state.dispatched_child_tasks or {}).get(uuid)
            if child_id:
                node.child_task_id = child_id
            pr = (self.state.pr_urls or {}).get(uuid)
            if pr:
                node.pr_url = pr
            cycles = (self.state.review_cycles or {}).get(uuid)
            if isinstance(cycles, int) and cycles > 0:
                node.review_cycles = cycles
            # AB-08: restore the pending review task id so `_poll_reviews`
            # can resume polling after a daemon restart. Merge verdict
            # into state only — there's no matching field on the Ticket
            # (the verdict is transient; once handled it drives a
            # status transition).
            rtid = (self.state.review_task_ids or {}).get(uuid)
            if rtid:
                node.review_task_id = rtid

    def _snapshot_graph_into_state(self) -> None:
        """Copy current ticket statuses + child ids onto `self.state` before
        we checkpoint. Also bumps `_ticket_transition_ts` for tickets whose
        status changed since the last snapshot so AB-05's stall watcher can
        measure time-in-state on the critical path.
        """
        now = time.time()
        for uuid, ticket in self.graph.nodes.items():
            prior = self.state.ticket_status.get(uuid)
            current = ticket.status.value
            if prior != current:
                self._ticket_transition_ts[uuid] = now
            # Seed transition_ts for first-seen tickets so a stall watcher
            # after a fresh restart has a reference point.
            self._ticket_transition_ts.setdefault(uuid, now)
            self.state.ticket_status[uuid] = current
            if ticket.child_task_id:
                self.state.dispatched_child_tasks[uuid] = ticket.child_task_id
            if ticket.pr_url:
                self.state.pr_urls[uuid] = ticket.pr_url
            if ticket.review_cycles:
                self.state.review_cycles[uuid] = ticket.review_cycles
            # AB-08: mirror pending review task ids into state so a restart
            # after a review was dispatched (but before its verdict landed)
            # still finds the task id on resume.
            if ticket.review_task_id:
                self.state.review_task_ids[uuid] = ticket.review_task_id

    # ── dispatch ────────────────────────────────────────────────────────────

    async def _dispatch_wave(self, wave_n: int) -> None:
        """Dispatch + poll tickets in `wave_n` until every one of them is in
        a terminal state. Inner loop = one 45s tick."""
        wave_tickets = self.graph.tickets_in_wave(wave_n)
        if not wave_tickets:
            logger.info("wave %d has no tickets; skipping", wave_n)
            return

        while True:
            # ── select ready ────────────────────────────────────────────
            in_flight = self._in_flight_for_wave(wave_n)
            ready = self._select_ready(wave_tickets, in_flight)

            # ── dispatch within caps ────────────────────────────────────
            for ticket in ready:
                # AB-05: in drain mode we let in-flight work finish but
                # stop selecting new children. `break` (not `continue`)
                # because the ready list is sorted critical-path first;
                # bailing early preserves the priority ordering if/when
                # drain is cleared.
                if self._drain_mode:
                    break
                if len(in_flight) >= self.max_parallel_subs:
                    break
                if self._epic_in_flight(ticket.epic, in_flight) >= self.per_epic_cap:
                    continue
                # SS-08 gate (AB-06 stub for now).
                if ticket.code.upper() == "SS-08":
                    allowed = await self._maybe_ss08_gate(ticket)
                    if not allowed:
                        continue
                try:
                    await self._dispatch_child(ticket)
                    in_flight.append(ticket)
                except Exception:
                    logger.exception(
                        "dispatch failed for %s; will retry next tick",
                        ticket.identifier,
                    )

            # ── poll children ───────────────────────────────────────────
            try:
                await self._poll_children()
            except Exception:
                logger.exception("poll_children failed; will retry next tick")

            # ── poll reviews (AB-08) ────────────────────────────────────
            # Must run AFTER _poll_children (which populates
            # `_last_completed_by_id`) and BEFORE _check_budget so review
            # task completion events land in the same spend-tally window
            # as child completions. Silent retries inside _poll_reviews
            # may re-fire review dispatches; that's fine — the new review
            # shows up next tick.
            try:
                await self._poll_reviews()
            except Exception:
                logger.exception("poll_reviews failed; will retry next tick")

            # ── periodic hooks ──────────────────────────────────────────
            await self._check_budget()
            await self._status_tick()
            try:
                await self._stall_watcher()
            except Exception:
                logger.exception("stall_watcher failed; continuing")

            # ── snapshot + checkpoint ───────────────────────────────────
            self._snapshot_graph_into_state()
            await checkpoint(self.state, self.soul, self.task_id)

            # ── exit condition ──────────────────────────────────────────
            if all(t.status in TERMINAL_STATES for t in wave_tickets):
                logger.info(
                    "wave %d all tickets terminal; breaking dispatch loop",
                    wave_n,
                )
                break

            await asyncio.sleep(self.poll_sleep_sec)

    def _select_ready(
        self,
        wave_tickets: List[Ticket],
        in_flight: List[Ticket],
    ) -> List[Ticket]:
        """Return tickets in `pending` whose `blocks_in` are all merged_green.

        Sort deterministically: critical-path first, then by identifier.
        """
        in_flight_ids = {t.id for t in in_flight}
        ready: List[Ticket] = []
        for t in wave_tickets:
            # Only PENDING or BLOCKED tickets can (re-)enter the dispatch
            # queue. Terminal + in-flight states are filtered out.
            if t.status not in (TicketStatus.PENDING, TicketStatus.BLOCKED):
                continue
            if t.id in in_flight_ids:
                continue
            if not self._deps_satisfied(t):
                if t.status != TicketStatus.BLOCKED:
                    # Explicitly mark blocked so the cadence report is honest.
                    t.status = TicketStatus.BLOCKED
                continue
            # Resurrect from BLOCKED if deps are now satisfied.
            if t.status == TicketStatus.BLOCKED:
                t.status = TicketStatus.PENDING
            ready.append(t)
        ready.sort(key=lambda x: (not x.is_critical_path, x.identifier))
        return ready

    def _deps_satisfied(self, ticket: Ticket) -> bool:
        for dep_id in ticket.blocks_in:
            dep = self.graph.nodes.get(dep_id)
            if dep is None:
                # Missing dep node — treat as satisfied rather than deadlocking
                # (cross-project or already closed historically).
                continue
            if dep.status != TicketStatus.MERGED_GREEN:
                return False
        return True

    def _in_flight_for_wave(self, wave_n: int) -> List[Ticket]:
        return [
            t for t in self.graph.tickets_in_wave(wave_n)
            if t.status in (
                TicketStatus.DISPATCHED,
                TicketStatus.IN_PROGRESS,
                TicketStatus.PR_OPEN,
                TicketStatus.REVIEWING,
                TicketStatus.MERGE_REQUESTED,
            )
        ]

    def _epic_in_flight(self, epic: str, in_flight: List[Ticket]) -> int:
        return sum(1 for t in in_flight if t.epic == epic)

    async def _dispatch_child(self, ticket: Ticket) -> None:
        """Create a `[persona:alfred-coo-a]` child mesh task for `ticket`,
        mark Linear `In Progress`, and stamp the ticket as dispatched.

        Uses `self.mesh.create_task(...)` — added to MeshClient alongside
        this orchestrator (plan F §4.2 notes mesh_task_create as re-used).
        """
        title = self._child_task_title(ticket)
        body = self._child_task_body(ticket)
        logger.info(
            "dispatching %s %s (wave %d, epic=%s, cp=%s)",
            ticket.identifier, ticket.code, ticket.wave,
            ticket.epic, ticket.is_critical_path,
        )
        resp = await self.mesh.create_task(
            title=title,
            description=body,
            from_session_id=self.settings.soul_session_id,
        )
        if not isinstance(resp, dict) or not resp.get("id"):
            raise RuntimeError(f"mesh create_task returned no id: {resp!r}")
        ticket.child_task_id = resp["id"]
        ticket.status = TicketStatus.DISPATCHED
        self.state.record_event(
            "ticket_dispatched",
            identifier=ticket.identifier,
            child_task_id=ticket.child_task_id,
        )

        # Linear: Todo -> In Progress via the AB-03 helper. Failure is
        # logged but non-fatal — orchestrator bookkeeping is the source of
        # truth; Linear state is a convenience mirror.
        await self._update_linear_state(ticket, "In Progress")

    def _child_task_title(self, ticket: Ticket) -> str:
        # Truncate the Linear title so the full tag stays readable.
        short = (ticket.title or "")[:80].rstrip()
        code = f" {ticket.code}" if ticket.code else ""
        return (
            f"[persona:alfred-coo-a] [wave-{ticket.wave}] [{ticket.epic}] "
            f"{ticket.identifier}{code} — {short}"
        )

    def _child_task_body(self, ticket: Ticket) -> str:
        """Build the APE/V acceptance block for the child. For AB-04 we
        embed a template + ticket facts; a future enhancement (AB-07 or
        later) can load the matching plan-doc section via http_get.

        AB-13 (Plan H §2 G-2): emits a ``## Target`` block pre-resolving
        ``{owner, repo, paths}`` from the static ``_TARGET_HINTS`` table,
        so the child no longer has to guess its target repo and path
        from the plan doc alone. Unmapped codes produce an
        ``(unresolved)`` block that tells the child to STOP and open a
        grounding-gap Linear issue per its Step 0 protocol.
        """
        plan_doc = self._plan_doc_for_epic(ticket.epic)
        size_line = f"Size: {ticket.size}" if ticket.size else "Size: unspecified"
        cp_line = " CRITICAL-PATH" if ticket.is_critical_path else ""
        # AB-14 (SAL-2699): emit the plan-doc code verbatim so the child can
        # grep the plan-doc markdown for its exact section anchor (F08, OPS-01,
        # C-26, ...). Empty-code tickets must escalate — the child has no
        # grounding and would otherwise fabricate scope.
        if ticket.code:
            plan_doc_code_line = (
                f"Plan-doc code: {ticket.code} "
                f"(search for this string in the plan-doc markdown)\n"
            )
        else:
            plan_doc_code_line = (
                "Plan-doc code: (unparseable — escalate per Step 0 of your "
                "persona protocol)\n"
            )
        # AB-13 (SAL-2698, Plan H §2 G-2): resolve target owner/repo/paths
        # up front via _TARGET_HINTS so the child knows which repo + which
        # files to edit. Unmapped codes emit an (unresolved) block telling
        # the child to open a grounding-gap Linear issue.
        target_block = _render_target_block(ticket.code)
        return (
            f"Ticket: {ticket.identifier} ({ticket.code or 'no-code'}){cp_line}\n"
            f"Linear: https://linear.app/saluca/issue/{ticket.identifier}\n"
            f"Wave: {ticket.wave}\n"
            f"Epic: {ticket.epic}\n"
            f"{size_line}\n"
            f"Estimate: {ticket.estimate}\n"
            f"Parent autonomous_build kickoff: {self.task_id}\n"
            f"{plan_doc_code_line}"
            f"\n"
            f"{target_block}"
            f"\n"
            f"## Acceptance (APE/V)\n"
            f"- [ ] Implementation matches the plan section for this ticket.\n"
            f"- [ ] Unit + integration tests added or updated.\n"
            f"- [ ] `ruff` + `pytest` green in CI.\n"
            f"- [ ] PR opened via `propose_pr`; orchestrator will dispatch a "
            f"hawkman-qa-a review on merge-ready.\n"
            f"- [ ] Structured output envelope includes the PR URL in "
            f"`summary` or `follow_up_tasks`.\n"
            f"\n"
            f"## Plan doc context\n"
            f"Plan doc (fetch via http_get): {plan_doc}\n"
            f"Pay attention to the section matching ticket code "
            f"{ticket.code or ticket.identifier}.\n"
            f"\n"
            f"## Deliverable\n"
            f"Open ONE PR to the target Saluca repo on a feature branch named "
            f"`feature/{ticket.identifier.lower()}-<short-slug>`. Respect the "
            f"APE/V block above. Keep the diff scoped to this ticket. The "
            f"`## Target` block above pins the repo + paths — do NOT edit "
            f"files outside those paths without opening a grounding-gap "
            f"Linear issue first.\n"
        )

    #: Base URL where v1-GA plan docs live in the alfred-coo-svc repo.
    #: Children run on Oracle and can't see minipc's Z:/ drive, so we emit
    #: repo-raw URLs they can fetch with `http_get`.
    _PLAN_DOC_BASE_URL = (
        "https://raw.githubusercontent.com/salucallc/alfred-coo-svc/main/"
        "plans/v1-ga"
    )

    #: Epic -> plan doc filename. Five v1-GA epics map to A..E; anything
    #: else falls back to the autonomous-build self-reference docs F and G.
    _EPIC_TO_PLAN_FILE = {
        "tiresias": "A_tiresias_in_appliance.md",
        "aletheia": "B_aletheia_daemon.md",
        "fleet": "C_fleet_mode_endpoint.md",
        "ops": "D_ops_layer.md",
        "soul-gap": "E_soul_svc_gaps.md",
    }

    @classmethod
    def _plan_doc_for_epic(cls, epic: str) -> str:
        """Return a raw.githubusercontent.com URL for the plan doc that
        matches this ticket's epic. Child alfred-coo-a tasks run on Oracle
        and must fetch the plan via `http_get`, so paths like
        ``Z:/_planning/v1-ga/*.md`` (minipc-only) won't resolve. Fallback
        for unknown epics points at the autonomous_build gap-closer plan
        (G), which lists orchestrator-side fixes — safer than a 404.
        """
        filename = cls._EPIC_TO_PLAN_FILE.get(
            epic, "G_autonomous_build_gap_closers.md"
        )
        return f"{cls._PLAN_DOC_BASE_URL}/{filename}"

    # ── child polling + state transitions ───────────────────────────────────

    async def _poll_children(self) -> List[Ticket]:
        """Query recently completed mesh tasks and match them back to
        dispatched tickets. Returns the tickets whose statuses changed this
        tick (useful for tests + future cadence diffing).
        """
        in_flight = [
            t for t in self.graph.nodes.values()
            if t.child_task_id
            and t.status not in TERMINAL_STATES
        ]
        if not in_flight:
            return []

        try:
            completed = await self.mesh.list_tasks(status="completed", limit=100)
        except Exception:
            logger.exception("mesh.list_tasks(completed) failed")
            return []
        by_id = {c.get("id"): c for c in (completed or []) if isinstance(c, dict)}
        # AB-05: expose the raw completed records for `_check_budget` to
        # walk without re-querying the mesh. We stash only the records that
        # correspond to tickets we actually dispatched (avoids double-
        # counting unrelated completed tasks sharing the mesh bus).
        self._last_completed_records = [
            by_id[t.child_task_id]
            for t in in_flight
            if t.child_task_id in by_id
        ]
        # AB-08: stash the full by_id dict so `_poll_reviews` can look up
        # review-task records without a second `list_tasks` round trip.
        # The list_tasks call above is not ticket-scoped, so this dict
        # covers child tasks AND review tasks in one batch. Safe to expose
        # in full; unrelated entries are ignored by the review poller.
        self._last_completed_by_id = dict(by_id)

        updated: List[Ticket] = []
        for ticket in in_flight:
            # AB-08 bug fix (2026-04-24): if the ticket is already past
            # PR_OPEN — i.e. already handed off to _poll_reviews — do NOT
            # re-process the same completed child record. Otherwise every
            # poll cycle re-fires _dispatch_review, spawning duplicate
            # review tasks and burning budget. Observed on v5 live run:
            # SAL-2634 got 15+ review tasks in 7 minutes before the patch.
            if ticket.status in (
                TicketStatus.REVIEWING,
                TicketStatus.MERGE_REQUESTED,
            ):
                continue
            rec = by_id.get(ticket.child_task_id)
            if rec is None:
                # Still in flight. Escalate status from DISPATCHED to
                # IN_PROGRESS if we see that the child has been claimed
                # (rough proxy — real impl would cross-check claimed_at).
                if ticket.status == TicketStatus.DISPATCHED:
                    ticket.status = TicketStatus.IN_PROGRESS
                continue

            task_status = (rec.get("status") or "").lower()
            result = rec.get("result") or {}
            if task_status == "failed":
                # Child errored out. Mark failed; the wave-gate logic decides
                # whether to halt.
                ticket.status = TicketStatus.FAILED
                self.state.record_event(
                    "ticket_failed",
                    identifier=ticket.identifier,
                    reason=(result.get("error") or "")[:200],
                )
                await self._update_linear_state(ticket, "Canceled")
                updated.append(ticket)
                continue

            # Successful completion. Look for a PR URL in the structured
            # envelope; missing URL → the child did QA/docs work only.
            pr_url = self._extract_pr_url(result)
            if pr_url:
                ticket.pr_url = pr_url
                ticket.status = TicketStatus.PR_OPEN
                self.state.record_event(
                    "ticket_pr_open",
                    identifier=ticket.identifier,
                    pr_url=pr_url,
                )
                # Fire a hawkman-qa-a review task asynchronously.
                try:
                    await self._dispatch_review(ticket)
                    ticket.status = TicketStatus.REVIEWING
                except Exception:
                    logger.exception(
                        "failed to dispatch review for %s",
                        ticket.identifier,
                    )
                updated.append(ticket)
            else:
                # No PR → child silently completed without producing a PR.
                # This is almost always a bug in the child persona (model did
                # not call propose_pr), NOT a success. Mark FAILED so the wave
                # gate catches it. Operator resets Linear state to Backlog to
                # retry (2026-04-23: observed 12 false-greens on first live
                # run; orchestrator marked MERGED_GREEN in this branch,
                # skipping the real claim→build→PR→review flow).
                ticket.status = TicketStatus.FAILED
                self.state.record_event(
                    "ticket_failed",
                    identifier=ticket.identifier,
                    note="child completed without PR URL",
                )
                await self._update_linear_state(ticket, "Backlog")
                updated.append(ticket)

        return updated

    @staticmethod
    def _extract_pr_url(result: Dict[str, Any]) -> Optional[str]:
        """Mine a PR URL out of the child task's `result` envelope.

        Child personas produce an envelope with `summary` + optional
        `follow_up_tasks` + optional `tool_calls`. We look in each of those
        fields for an https://github.com/.../pull/<n> link.
        """
        if not isinstance(result, dict):
            return None

        candidates: List[str] = []
        for key in ("summary", "content"):
            val = result.get(key)
            if isinstance(val, str):
                candidates.append(val)
        # tool_calls may contain propose_pr responses with a pr_url field.
        tc = result.get("tool_calls") or []
        if isinstance(tc, list):
            for call in tc:
                if not isinstance(call, dict):
                    continue
                out = call.get("result") or call.get("output") or {}
                if isinstance(out, dict):
                    pr = out.get("pr_url")
                    if isinstance(pr, str):
                        candidates.append(pr)
                elif isinstance(out, str):
                    candidates.append(out)
        follow = result.get("follow_up_tasks") or []
        if isinstance(follow, list):
            for f in follow:
                if isinstance(f, str):
                    candidates.append(f)
                elif isinstance(f, dict):
                    v = f.get("url") or f.get("pr_url") or ""
                    if v:
                        candidates.append(str(v))
        for cand in candidates:
            m = _PR_URL_RE.search(cand)
            if m:
                return m.group(0)
        return None

    async def _dispatch_review(self, ticket: Ticket) -> None:
        """Fire a `[persona:hawkman-qa-a]` child task to review the PR.

        AB-08: stashes the new mesh task id on `ticket.review_task_id` +
        `state.review_task_ids` BEFORE returning so `_poll_reviews` can
        pick up the verdict on the next tick. Does NOT bump
        `review_cycles` — that counter is the number of REQUEST_CHANGES
        cycles already observed, managed by the verdict handler.
        """
        # Human-readable cycle number for the title: 1-indexed, so the
        # first review is "cycle #1".
        cycle_display = ticket.review_cycles + 1
        title = (
            f"[persona:hawkman-qa-a] [wave-{ticket.wave}] [{ticket.epic}] "
            f"review {ticket.identifier} {ticket.code} "
            f"(cycle #{cycle_display})"
        )
        body = (
            f"Independent APE/V review of PR for {ticket.identifier}.\n"
            f"PR: {ticket.pr_url}\n"
            f"Parent autonomous_build: {self.task_id}\n"
            f"\n"
            f"Use constrained prompt: 2-tool-call budget, <300 char body.\n"
            f"Approve with APPROVE; else REQUEST_CHANGES with actionable notes.\n"
        )
        resp = await self.mesh.create_task(
            title=title,
            description=body,
            from_session_id=self.settings.soul_session_id,
        )
        if isinstance(resp, dict):
            review_task_id = resp.get("id")
            if review_task_id:
                # AB-08: stash the id on the ticket + state BEFORE the
                # orchestrator transitions to REVIEWING so a checkpoint
                # taken mid-tick contains the pending review pointer.
                ticket.review_task_id = str(review_task_id)
                self.state.review_task_ids[ticket.id] = str(review_task_id)
            self.state.record_event(
                "review_dispatched",
                identifier=ticket.identifier,
                review_task_id=review_task_id,
                cycle=cycle_display,
            )

    # ── AB-08: review verdict loop ──────────────────────────────────────────

    @staticmethod
    def _extract_verdict(result: Dict[str, Any]) -> Optional[str]:
        """Mine a verdict out of the review task's `result` envelope.

        Priority (matches AB-08 design doc §4):

        1. ``result.tool_calls[*].result.state`` where the tool was
           ``pr_review`` (values: ``APPROVE`` / ``REQUEST_CHANGES`` /
           ``COMMENT`` / ``COMMENTED_FALLBACK``).
        2. Regex ``\\bAPPROVE\\b`` / ``\\bREQUEST_CHANGES\\b`` on
           ``result.summary``.
        3. Same regex on ``result.follow_up_tasks`` (string or
           list-of-strings).

        Returns ``None`` when nothing parseable is found — caller treats
        that as silent and retries once.
        """
        if not isinstance(result, dict):
            return None

        # Priority 1: structured tool-call result.
        tc = result.get("tool_calls") or []
        if isinstance(tc, list):
            for call in tc:
                if not isinstance(call, dict):
                    continue
                if (call.get("name") or "").lower() != "pr_review":
                    continue
                out = call.get("result") or call.get("output") or {}
                if not isinstance(out, dict):
                    continue
                state = out.get("state")
                if isinstance(state, str) and state:
                    return state.upper()

        # Priority 2: summary regex.
        summary = result.get("summary")
        if isinstance(summary, str) and summary:
            if _VERDICT_REQUEST_CHANGES_RE.search(summary):
                return "REQUEST_CHANGES"
            if _VERDICT_APPROVE_RE.search(summary):
                return "APPROVE"

        # Priority 3: follow_up_tasks scan.
        follow = result.get("follow_up_tasks")
        follow_strs: List[str] = []
        if isinstance(follow, str):
            follow_strs.append(follow)
        elif isinstance(follow, list):
            for f in follow:
                if isinstance(f, str):
                    follow_strs.append(f)
                elif isinstance(f, dict):
                    # Accept common shapes {"summary": "..."} / {"title": "..."}.
                    for key in ("summary", "title", "text"):
                        v = f.get(key)
                        if isinstance(v, str) and v:
                            follow_strs.append(v)
        for blob in follow_strs:
            if _VERDICT_REQUEST_CHANGES_RE.search(blob):
                return "REQUEST_CHANGES"
            if _VERDICT_APPROVE_RE.search(blob):
                return "APPROVE"

        return None

    @staticmethod
    def _parse_fallback_verdict(rec: Dict[str, Any]) -> Optional[str]:
        """Extract the ``intended_event`` from a ``COMMENTED_FALLBACK``
        ``pr_review`` tool-call payload.

        When ``pr_review`` can't submit a real PR review (422 self-
        authored fallback, tools.py:500-512) it still returns
        ``intended_event`` with the verdict the reviewer tried to land.
        This helper plucks that out so the orchestrator can treat it as
        a real verdict. Returns None if the fallback payload is missing
        or ambiguous — caller treats that as silent.
        """
        result = rec.get("result") if isinstance(rec, dict) else None
        if not isinstance(result, dict):
            return None
        tc = result.get("tool_calls") or []
        if not isinstance(tc, list):
            return None
        for call in tc:
            if not isinstance(call, dict):
                continue
            if (call.get("name") or "").lower() != "pr_review":
                continue
            out = call.get("result") or call.get("output") or {}
            if not isinstance(out, dict):
                continue
            if (out.get("state") or "").upper() != "COMMENTED_FALLBACK":
                continue
            intended = out.get("intended_event")
            if isinstance(intended, str) and intended:
                return intended.upper()
        return None

    async def _poll_reviews(self) -> List[Ticket]:
        """Walk REVIEWING tickets; drive each toward MERGED_GREEN or FAILED.

        Consumes ``self._last_completed_by_id`` (populated by
        ``_poll_children`` on the same tick). Review tasks still in
        flight are skipped; completed ones have their verdict extracted
        and acted on:

        - **APPROVE** → mark MERGE_REQUESTED, call ``_merge_pr``. On
          success: MERGED_GREEN + Linear Done. On failure: FAILED.
        - **REQUEST_CHANGES** → check cap; if under the cap, increment
          ``review_cycles`` and ``_respawn_child_with_fixes``; else FAILED.
        - **COMMENTED_FALLBACK** → parse ``intended_event``; recurse into
          the matching branch or fall through to silent.
        - **None (silent)** → bump ``silent_review_retries``; retry once
          by re-firing ``_dispatch_review``. Second silent → FAILED.

        Returns tickets whose status changed this tick (useful for
        tests + cadence diffing).
        """
        by_id = self._last_completed_by_id or {}
        reviewing = [
            t for t in self.graph.nodes.values()
            if t.status == TicketStatus.REVIEWING and t.review_task_id
        ]
        if not reviewing:
            return []

        updated: List[Ticket] = []
        for ticket in reviewing:
            rec = by_id.get(ticket.review_task_id)
            if rec is None:
                # Review still in flight — skip this tick.
                continue
            result = rec.get("result") or {}
            verdict = self._extract_verdict(result)
            await self._handle_review_verdict(ticket, rec, verdict, updated)
        return updated

    async def _handle_review_verdict(
        self,
        ticket: Ticket,
        rec: Dict[str, Any],
        verdict: Optional[str],
        updated: List[Ticket],
    ) -> None:
        """Dispatch one review verdict. Broken out of ``_poll_reviews`` so
        the COMMENTED_FALLBACK branch can recurse cleanly with a parsed
        verdict without reshaping the caller's loop.
        """
        result = rec.get("result") or {}

        # Record the extracted verdict (best-effort — None = silent).
        if verdict:
            self.state.review_verdicts[ticket.id] = verdict

        if verdict == "APPROVE":
            ticket.status = TicketStatus.MERGE_REQUESTED
            merged = await self._merge_pr(ticket)
            if merged:
                ticket.status = TicketStatus.MERGED_GREEN
                self.state.record_event(
                    "ticket_merged",
                    identifier=ticket.identifier,
                    pr_url=ticket.pr_url,
                    sha=self.state.merged_pr_urls.get(ticket.id),
                )
                await self._update_linear_state(ticket, "Done")
            else:
                ticket.status = TicketStatus.FAILED
                self.state.record_event(
                    "ticket_merge_failed",
                    identifier=ticket.identifier,
                    pr_url=ticket.pr_url,
                )
                await self._update_linear_state(ticket, "Backlog")
            updated.append(ticket)
            return

        if verdict == "REQUEST_CHANGES":
            if ticket.review_cycles >= MAX_REVIEW_CYCLES:
                ticket.status = TicketStatus.FAILED
                self.state.record_event(
                    "review_max_cycles",
                    identifier=ticket.identifier,
                    cycles=ticket.review_cycles,
                )
                await self._update_linear_state(ticket, "Backlog")
                updated.append(ticket)
                return
            # Under cap — spawn a fresh child with the review feedback.
            review_body = self._extract_review_body(result)
            ticket.review_cycles += 1
            # Clear the stale review task pointer so the next PR_OPEN can
            # cleanly seed a fresh review round via `_dispatch_review`.
            ticket.review_task_id = None
            self.state.review_task_ids.pop(ticket.id, None)
            await self._respawn_child_with_fixes(ticket, review_body)
            ticket.status = TicketStatus.DISPATCHED
            self.state.record_event(
                "ticket_respawned",
                identifier=ticket.identifier,
                cycle=ticket.review_cycles,
                child_task_id=ticket.child_task_id,
            )
            updated.append(ticket)
            return

        if verdict == "COMMENTED_FALLBACK":
            parsed = self._parse_fallback_verdict(rec)
            if parsed in ("APPROVE", "REQUEST_CHANGES"):
                # Trust intended_event — recurse with the parsed verdict.
                await self._handle_review_verdict(
                    ticket, rec, parsed, updated
                )
                return
            # COMMENT-ish fallback with no actionable intent → silent path.
            verdict = None

        # Silent / ambiguous branch.
        ticket.silent_review_retries += 1
        if ticket.silent_review_retries > 1:
            ticket.status = TicketStatus.FAILED
            self.state.record_event(
                "review_silent_failed",
                identifier=ticket.identifier,
                retries=ticket.silent_review_retries,
            )
            await self._update_linear_state(ticket, "Backlog")
            updated.append(ticket)
            return
        # First silent miss → re-fire the review.
        self.state.record_event(
            "review_silent_retry",
            identifier=ticket.identifier,
            retries=ticket.silent_review_retries,
        )
        # Clear the stale task id first so the new dispatch overwrites it.
        ticket.review_task_id = None
        self.state.review_task_ids.pop(ticket.id, None)
        try:
            await self._dispatch_review(ticket)
            # _dispatch_review doesn't flip status; keep it REVIEWING so
            # the next tick sees the new review_task_id and re-checks.
            ticket.status = TicketStatus.REVIEWING
        except Exception:
            logger.exception(
                "silent-retry _dispatch_review failed for %s",
                ticket.identifier,
            )
        updated.append(ticket)

    @staticmethod
    def _extract_review_body(result: Dict[str, Any]) -> str:
        """Mine the review's textual feedback out of the result envelope.

        Looks at tool_calls[pr_review].result.body / .html_url first, then
        ``summary``, then ``follow_up_tasks``. Returns an empty string
        when nothing useful is present (respawn still fires, just without
        an embedded review excerpt).
        """
        if not isinstance(result, dict):
            return ""
        # Tool-call body.
        tc = result.get("tool_calls") or []
        if isinstance(tc, list):
            for call in tc:
                if not isinstance(call, dict):
                    continue
                if (call.get("name") or "").lower() != "pr_review":
                    continue
                out = call.get("result") or call.get("output") or {}
                if isinstance(out, dict):
                    for key in ("body", "review_body", "html_url"):
                        v = out.get(key)
                        if isinstance(v, str) and v.strip():
                            return v
        # Summary.
        summary = result.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary
        # follow_up_tasks fallback.
        follow = result.get("follow_up_tasks") or []
        if isinstance(follow, list):
            parts: List[str] = []
            for f in follow:
                if isinstance(f, str):
                    parts.append(f)
                elif isinstance(f, dict):
                    for key in ("summary", "title", "text"):
                        v = f.get(key)
                        if isinstance(v, str) and v:
                            parts.append(v)
            if parts:
                return "\n".join(parts)
        return ""

    async def _merge_pr(self, ticket: Ticket) -> bool:
        """Merge `ticket.pr_url` via the AB-10 ``github_merge_pr`` tool.

        Returns True on success (including the double-merge guard hit);
        False otherwise. Stashes the merge SHA on
        ``state.merged_pr_urls[ticket.id]`` for idempotency on restart.

        Double-merge guard: if the ticket is already MERGED_GREEN or
        already has an entry in ``merged_pr_urls``, short-circuit True
        without calling GitHub. This makes restart-resume idempotent:
        a daemon that died between the GitHub PUT and the status
        transition will see the entry on restore and skip the re-merge.
        """
        # Double-merge guard — restart-idempotent.
        if (
            ticket.status == TicketStatus.MERGED_GREEN
            or ticket.id in self.state.merged_pr_urls
        ):
            logger.info(
                "skipping re-merge for %s (already merged, sha=%s)",
                ticket.identifier,
                self.state.merged_pr_urls.get(ticket.id),
            )
            return True

        if not ticket.pr_url:
            logger.warning(
                "cannot merge %s: no pr_url on ticket",
                ticket.identifier,
            )
            return False

        m = _PR_URL_RE.search(ticket.pr_url)
        if not m:
            logger.warning(
                "cannot merge %s: pr_url %r does not match expected format",
                ticket.identifier, ticket.pr_url,
            )
            return False

        # _PR_URL_RE is the broad orchestrator version; parse owner/repo/num
        # from the matched URL with a tighter regex so we get the groups.
        parsed = re.match(
            r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)",
            m.group(0),
        )
        if parsed is None:
            logger.warning(
                "cannot merge %s: pr_url parse failed", ticket.identifier,
            )
            return False
        owner, repo, pr_num_str = parsed.group(1), parsed.group(2), parsed.group(3)
        try:
            pr_num = int(pr_num_str)
        except (TypeError, ValueError):
            logger.warning(
                "cannot merge %s: pr number %r not int",
                ticket.identifier, pr_num_str,
            )
            return False

        try:
            from alfred_coo.tools import BUILTIN_TOOLS
        except Exception:
            logger.exception("tools not importable; cannot merge")
            return False
        spec = BUILTIN_TOOLS.get("github_merge_pr")
        if spec is None:
            logger.error(
                "github_merge_pr missing from BUILTIN_TOOLS; "
                "cannot merge %s",
                ticket.identifier,
            )
            return False

        try:
            resp = await spec.handler(
                owner=owner, repo=repo, pr_number=pr_num,
                merge_method="squash",
            )
        except Exception:
            logger.exception(
                "github_merge_pr raised for %s (%s)",
                ticket.identifier, ticket.pr_url,
            )
            return False

        if not isinstance(resp, dict):
            logger.warning(
                "github_merge_pr returned non-dict for %s: %r",
                ticket.identifier, resp,
            )
            return False

        if not resp.get("ok"):
            logger.warning(
                "github_merge_pr failed for %s: %r",
                ticket.identifier, resp,
            )
            return False

        sha = resp.get("sha")
        self.state.merged_pr_urls[ticket.id] = (
            str(sha) if sha else str(ticket.pr_url)
        )
        return True

    async def _respawn_child_with_fixes(
        self,
        ticket: Ticket,
        review_body: str,
    ) -> None:
        """Create a fresh alfred-coo-a child task seeded with review feedback.

        The new child is expected to push fixes to the SAME branch so the
        existing PR picks them up automatically (no new PR). The reviewer
        bot re-reviews on the next tick once the new child completes and
        `_poll_children` re-enters PR_OPEN → REVIEWING.

        Also resets ``ticket.silent_review_retries`` because that counter
        is scoped to one review attempt, not the whole build cycle.
        """
        # Truncate to keep the body reasonable — hawkman feedback can be
        # verbose. Keep the first 4KB; the full review is still in soul
        # memory / the mesh task record if the builder needs more.
        max_body_chars = 4096
        review_excerpt = (review_body or "").strip()
        if len(review_excerpt) > max_body_chars:
            review_excerpt = (
                review_excerpt[:max_body_chars]
                + f"\n[...truncated {len(review_excerpt) - max_body_chars} "
                + "chars; see review task for full content]"
            )

        short_title = (ticket.title or "")[:80].rstrip()
        code = f" {ticket.code}" if ticket.code else ""
        # `review_cycles` is already incremented by the verdict handler
        # before this respawn fires, so it is the round number of THIS
        # fix attempt (1 = first fix after the initial review).
        round_num = ticket.review_cycles
        title = (
            f"[persona:alfred-coo-a] [wave-{ticket.wave}] [{ticket.epic}] "
            f"{ticket.identifier}{code} — fix: round {round_num} "
            f"({short_title})"
        )[:220]  # mesh task title practical cap

        plan_doc = self._plan_doc_for_epic(ticket.epic)
        cp_line = " CRITICAL-PATH" if ticket.is_critical_path else ""
        body = (
            f"Ticket: {ticket.identifier} ({ticket.code or 'no-code'}){cp_line}\n"
            f"Linear: https://linear.app/saluca/issue/{ticket.identifier}\n"
            f"Wave: {ticket.wave}\n"
            f"Epic: {ticket.epic}\n"
            f"Parent autonomous_build kickoff: {self.task_id}\n"
            f"Previous PR: {ticket.pr_url}\n"
            f"Review round: {round_num} of {MAX_REVIEW_CYCLES}\n"
            f"\n"
            f"## Acceptance (APE/V)\n"
            f"- [ ] Address every point in the review feedback below.\n"
            f"- [ ] Tests still green (`ruff` + `pytest`).\n"
            f"- [ ] Push fixes to the EXISTING branch for {ticket.pr_url}; "
            f"do NOT open a new PR. The reviewer bot will re-review "
            f"automatically once your new commit lands.\n"
            f"\n"
            f"## Review feedback\n"
            f"{review_excerpt or _NO_REVIEW_BODY_NOTE}\n"
            f"\n"
            f"## Plan doc context\n"
            f"{plan_doc}\n"
            f"\n"
            f"## Instructions\n"
            f"Push fixes to existing branch; do NOT open a new PR. "
            f"The reviewer bot will re-review automatically.\n"
        )

        resp = await self.mesh.create_task(
            title=title,
            description=body,
            from_session_id=self.settings.soul_session_id,
        )
        if not isinstance(resp, dict) or not resp.get("id"):
            raise RuntimeError(
                f"mesh create_task returned no id for respawn: {resp!r}"
            )
        ticket.child_task_id = str(resp["id"])
        # Silent-retry counter is per-review-attempt, not per-ticket. A
        # fresh child gets a fresh silent-retry budget.
        ticket.silent_review_retries = 0

    # ── wave gate ───────────────────────────────────────────────────────────

    async def _wait_for_wave_gate(self, wave_n: int) -> None:
        """Block until every ticket in `wave_n` is terminal. Raise if a
        critical-path ticket failed; allow soft-green on non-critical
        failures if ≥`SOFT_GREEN_THRESHOLD` of the wave merged green."""
        wave_tickets = self.graph.tickets_in_wave(wave_n)
        if not wave_tickets:
            return
        while not all(t.status in TERMINAL_STATES for t in wave_tickets):
            await asyncio.sleep(self.poll_sleep_sec)
            # Drive the loop forward — in real operation this would be the
            # dispatch loop doing the work. In tests we advance statuses
            # directly between ticks.

        # Wave is terminal. Classify.
        failed = [t for t in wave_tickets if t.status == TicketStatus.FAILED]
        cp_failed = [t for t in failed if t.is_critical_path]
        green = [t for t in wave_tickets if t.status == TicketStatus.MERGED_GREEN]
        green_ratio = len(green) / max(1, len(wave_tickets))

        if cp_failed:
            msg = (
                f"wave {wave_n} has {len(cp_failed)} critical-path failure(s): "
                + ", ".join(t.identifier for t in cp_failed)
            )
            logger.error(msg)
            self.state.record_event("wave_halt_critical_path", wave=wave_n,
                                    failed=[t.identifier for t in cp_failed])
            raise RuntimeError(msg)

        if failed and green_ratio >= SOFT_GREEN_THRESHOLD:
            logger.warning(
                "wave %d soft-green: %d/%d merged, non-critical failures: %s",
                wave_n, len(green), len(wave_tickets),
                [t.identifier for t in failed],
            )
            self.state.record_event(
                "wave_soft_green",
                wave=wave_n,
                failed=[t.identifier for t in failed],
                green_ratio=green_ratio,
            )
            return

        if failed:
            msg = (
                f"wave {wave_n} failed: green_ratio={green_ratio:.2f} < "
                f"{SOFT_GREEN_THRESHOLD} and {len(failed)} non-critical failure(s)"
            )
            logger.error(msg)
            self.state.record_event(
                "wave_halt_below_soft_green",
                wave=wave_n,
                failed=[t.identifier for t in failed],
                green_ratio=green_ratio,
            )
            raise RuntimeError(msg)

        logger.info("wave %d all-green", wave_n)
        self.state.record_event("wave_all_green", wave=wave_n)

    # ── on-all-green actions ────────────────────────────────────────────────

    async def _run_on_all_green_actions(self) -> None:
        actions = self.payload.get("on_all_green") or []
        if not isinstance(actions, list) or not actions:
            return
        for action in actions:
            if not isinstance(action, str) or not action.strip():
                continue
            title = (
                f"[persona:alfred-coo-a] [v1-ga-finalize] "
                f"on_all_green: {action[:80]}"
            )
            body = (
                f"Parent autonomous_build kickoff: {self.task_id}\n"
                f"Action: {action}\n"
                f"\n"
                f"Execute this on_all_green action for Mission Control v1.0 GA. "
                f"Use the appropriate tools (propose_pr / slack_post / "
                f"http_get). Stay within scope.\n"
            )
            try:
                await self.mesh.create_task(
                    title=title,
                    description=body,
                    from_session_id=self.settings.soul_session_id,
                )
                self.state.record_event("on_all_green_dispatched", action=action)
            except Exception:
                logger.exception(
                    "failed to dispatch on_all_green action: %s", action
                )

    # ── stubs for later AB tickets ──────────────────────────────────────────

    async def _status_tick(self) -> None:
        """Rate-limited status log + Slack cadence post (AB-05).

        The log line mirrors the AB-04 format so operational `grep` works
        the same; the Slack post is delegated to `SlackCadence.tick`,
        which applies its own rate limit (matches `status_cadence_min`).
        """
        now = time.time()
        interval_sec = max(60, self.status_cadence_min * 60)
        if now - self._last_cadence_ts < interval_sec:
            return
        self._last_cadence_ts = now
        self.state.last_cadence_ts = now
        wave = self.state.current_wave
        wave_tickets = self.graph.tickets_in_wave(wave)
        green = sum(1 for t in wave_tickets if t.status == TicketStatus.MERGED_GREEN)
        total = len(wave_tickets)
        in_flight = len(self._in_flight_for_wave(wave))
        logger.info(
            "[cadence] wave=%d tickets=%d/%d in_flight=%d spend=$%.2f/$%.2f",
            wave, green, total, in_flight,
            self.state.cumulative_spend_usd,
            self.budget_usd,
        )
        try:
            await self.cadence.tick(
                self.state, self.graph, self.budget_tracker.status()
            )
        except Exception:
            logger.exception("SlackCadence.tick failed; continuing")

    async def _check_budget(self) -> None:
        """AB-05: aggregate token spend from the last poll batch, update
        `state.cumulative_spend_usd`, and trigger warn / hard-stop Slack
        posts at the configured thresholds.

        Operates on `self._last_completed_records` populated by the most
        recent `_poll_children` call. Each record is passed to the tracker,
        which is tolerant of missing `tokens`/`model` fields.
        """
        records = list(self._last_completed_records or [])
        # Clear early so the same batch can't be double-counted on the next
        # tick before the next _poll_children call repopulates it.
        self._last_completed_records = []

        if records:
            for rec in records:
                try:
                    self.budget_tracker.record(rec)
                except Exception:
                    logger.exception(
                        "budget_tracker.record raised; continuing on next record"
                    )
            # Mirror the tracker's cumulative spend onto state so the
            # soul-memory checkpoint stays authoritative.
            self.state.cumulative_spend_usd = self.budget_tracker.cumulative_spend

        # Threshold transitions. `check_warn` + `check_hard_stop` both
        # have one-shot semantics; calling them every tick is safe and
        # cheap.
        if self.budget_tracker.check_warn():
            warn_msg = (
                f":warning: [autonomous_build] budget 80% threshold hit: "
                f"${self.budget_tracker.cumulative_spend:.2f} / "
                f"${self.budget_tracker.max_usd:.2f}. Monitoring closely; "
                f"no new dispatch change yet."
            )
            self.state.record_event(
                "budget_warn",
                spend=self.budget_tracker.cumulative_spend,
                cap=self.budget_tracker.max_usd,
            )
            try:
                await self.cadence.post(warn_msg)
            except Exception:
                logger.exception("cadence.post(warn) failed; continuing")

        if self.budget_tracker.check_hard_stop():
            self._drain_mode = True
            stop_msg = (
                f":stop_sign: [autonomous_build] BUDGET HARD STOP at "
                f"${self.budget_tracker.cumulative_spend:.2f} "
                f"(cap ${self.budget_tracker.max_usd:.2f}). Drain mode: "
                f"in-flight drain, no new dispatches. Orchestrator will "
                f"complete current wave then halt."
            )
            self.state.record_event(
                "budget_hard_stop",
                spend=self.budget_tracker.cumulative_spend,
                cap=self.budget_tracker.max_usd,
            )
            try:
                await self.cadence.post(stop_msg)
            except Exception:
                logger.exception("cadence.post(hard_stop) failed; continuing")
            # Checkpoint immediately so a restart after a budget halt
            # sees the drain flag's side effects persisted.
            try:
                await checkpoint(self.state, self.soul, self.task_id)
            except Exception:
                logger.exception("post-hard-stop checkpoint failed; continuing")

    async def _stall_watcher(self) -> None:
        """Scan in-flight critical-path tickets; ping Slack if any has been
        in a non-terminal in-flight state for longer than
        `self.stall_threshold_sec`.

        Each ticket is pinged at most once per stall event — the
        `_stall_pinged` dict tracks last-ping ts per ticket. If the
        ticket transitions out of the stalled status, `_snapshot_graph_into_state`
        refreshes its `_ticket_transition_ts` and a future stall would
        re-arm the ping.
        """
        now = time.time()
        in_flight_states = {
            TicketStatus.DISPATCHED,
            TicketStatus.IN_PROGRESS,
            TicketStatus.PR_OPEN,
            TicketStatus.REVIEWING,
            TicketStatus.MERGE_REQUESTED,
        }
        threshold = max(60, int(self.stall_threshold_sec))

        for uuid, ticket in self.graph.nodes.items():
            if not ticket.is_critical_path:
                continue
            if ticket.status not in in_flight_states:
                # Ticket moved out of an in-flight state; clear the ping
                # marker so a fresh stall later re-arms.
                self._stall_pinged.pop(uuid, None)
                continue
            entered_ts = self._ticket_transition_ts.get(uuid)
            if entered_ts is None:
                continue
            elapsed = now - entered_ts
            if elapsed < threshold:
                continue
            # Already pinged for this specific stall window? Skip.
            if self._stall_pinged.get(uuid, 0.0) >= entered_ts:
                continue
            # Find the last event for this ticket, if any.
            last_event = ""
            for evt in reversed(self.state.events or []):
                if not isinstance(evt, dict):
                    continue
                if evt.get("identifier") == ticket.identifier:
                    last_event = f"{evt.get('kind', '?')} ({evt.get('identifier')})"
                    break
            try:
                await self.cadence.critical_path_ping(
                    ticket, int(elapsed), last_event
                )
                self._stall_pinged[uuid] = entered_ts
                self.state.record_event(
                    "critical_path_stall_ping",
                    identifier=ticket.identifier,
                    elapsed_sec=int(elapsed),
                )
            except Exception:
                logger.exception(
                    "critical_path_ping raised for %s; will retry next tick",
                    ticket.identifier,
                )

    async def _maybe_ss08_gate(self, ticket: Ticket) -> bool:
        """SS-08 gate: post JWS claims schema + poll #batcave for ACK.

        AB-06 implementation. Contract:
          - Non-SS-08 tickets: no-op, return True.
          - `self.state.ss08_acked` already True: skip gate, return True.
          - Otherwise run `run_ss08_gate(cadence, slack_ack_poll_fn)`:
              * On ACK: set `state.ss08_acked = True`, checkpoint,
                return True (dispatch proceeds).
              * On 4h timeout: mark ticket FAILED, record event,
                checkpoint, return False (skip + defer to v1.1 per D2).
              * On gate crash: log, mark FAILED, return False.
        """
        if ticket.code.upper() != "SS-08":
            return True
        if self.state.ss08_acked:
            logger.info(
                "SS-08 already acked in state; skipping gate for %s",
                ticket.identifier,
            )
            return True

        # Lazy import avoids forcing ss08_gate into the orchestrator's
        # import graph for tests that never touch SS-08 tickets.
        from .ss08_gate import run_ss08_gate

        # Resolve the real `slack_ack_poll` handler. Tests that exercise
        # the gate path inject a fake via monkeypatching
        # `orchestrator._resolve_slack_ack_poll` or stubbing
        # `BUILTIN_TOOLS`; AB-07 dry-run/smoke flips this to a no-op.
        try:
            poll_fn = self._resolve_slack_ack_poll()
        except Exception as e:
            logger.exception(
                "failed to resolve slack_ack_poll for SS-08 gate: %s", e
            )
            ticket.status = TicketStatus.FAILED
            self.state.record_event(
                "ss08_gate_resolve_failed",
                identifier=ticket.identifier,
                error=f"{type(e).__name__}: {str(e)[:200]}",
            )
            await checkpoint(self.state, self.soul, self.task_id)
            return False

        try:
            acked = await run_ss08_gate(
                cadence=self.cadence,
                slack_ack_poll_fn=poll_fn,
                logger_=logger,
            )
        except Exception as e:
            logger.exception("SS-08 gate errored: %s", e)
            ticket.status = TicketStatus.FAILED
            self.state.record_event(
                "ss08_gate_crashed",
                identifier=ticket.identifier,
                error=f"{type(e).__name__}: {str(e)[:200]}",
            )
            await checkpoint(self.state, self.soul, self.task_id)
            return False

        self.state.ss08_acked = bool(acked)
        await checkpoint(self.state, self.soul, self.task_id)

        if not acked:
            # D2: defer SS-08 to v1.1 on timeout. Marking the ticket
            # FAILED keeps the wave-gate soft-green logic honest: if
            # SS-08 is critical-path the orchestrator will halt; if
            # non-critical it can still clear the wave with a warning.
            ticket.status = TicketStatus.FAILED
            self.state.record_event(
                "ss08_gate_timeout",
                identifier=ticket.identifier,
                note="marked deferred v1.1",
            )
            await checkpoint(self.state, self.soul, self.task_id)
            return False

        self.state.record_event(
            "ss08_gate_acked",
            identifier=ticket.identifier,
        )
        return True

    def _resolve_slack_ack_poll(self):
        """Return the callable used by `run_ss08_gate` to poll Slack.

        Default resolution goes through `BUILTIN_TOOLS["slack_ack_poll"].handler`.
        Kept as a dedicated method so AB-07 (dry-run/smoke) can override
        via a simple `orch._resolve_slack_ack_poll = lambda: fake_fn`
        without reaching into BUILTIN_TOOLS.
        """
        from alfred_coo.tools import BUILTIN_TOOLS

        spec = BUILTIN_TOOLS.get("slack_ack_poll")
        if spec is None:
            raise RuntimeError(
                "slack_ack_poll tool missing from BUILTIN_TOOLS; "
                "cannot run SS-08 gate"
            )
        return spec.handler

    # ── Linear bookkeeping ──────────────────────────────────────────────────

    async def _update_linear_state(self, ticket: Ticket, state_name: str) -> None:
        """Mirror the ticket's orchestrator status to Linear via AB-03.
        Failure is logged + swallowed — our graph is source of truth."""
        try:
            from alfred_coo.tools import BUILTIN_TOOLS
        except Exception:
            logger.debug("tools not importable; skipping Linear update")
            return
        spec = BUILTIN_TOOLS.get("linear_update_issue_state")
        if spec is None:
            return
        try:
            resp = await spec.handler(issue_id=ticket.id, state_name=state_name)
            if isinstance(resp, dict) and resp.get("error"):
                logger.warning(
                    "linear_update_issue_state(%s, %s) returned error: %s",
                    ticket.identifier, state_name, resp["error"],
                )
        except Exception:
            logger.exception(
                "linear_update_issue_state raised for %s -> %s",
                ticket.identifier, state_name,
            )

    # ── kickoff termination ─────────────────────────────────────────────────

    async def _complete_kickoff(self) -> None:
        """Mark the kickoff mesh task complete with a final summary."""
        summary = self._build_final_summary()
        try:
            await self.mesh.complete(
                self.task_id,
                session_id=self.settings.soul_session_id,
                result={
                    "summary": summary["text"],
                    "stats": summary["stats"],
                    "final_state_snapshot": summary["state"],
                },
            )
        except Exception:
            logger.exception(
                "failed to mark kickoff task %s complete", self.task_id
            )

    async def _fail_kickoff(self, *, reason: str) -> None:
        """Mark the kickoff task failed with a state dump."""
        self._snapshot_graph_into_state()
        try:
            await self.mesh.complete(
                self.task_id,
                session_id=self.settings.soul_session_id,
                status="failed",
                result={
                    "error": reason,
                    "final_state_snapshot": {
                        "current_wave": self.state.current_wave,
                        "cumulative_spend_usd": self.state.cumulative_spend_usd,
                        "ticket_status": self.state.ticket_status,
                        "events_tail": self.state.events[-10:],
                    },
                },
            )
        except Exception:
            logger.exception(
                "failed to mark kickoff task %s failed", self.task_id
            )

    def _build_final_summary(self) -> Dict[str, Any]:
        self._snapshot_graph_into_state()
        total = len(self.graph)
        green = sum(1 for t in self.graph if t.status == TicketStatus.MERGED_GREEN)
        failed = sum(1 for t in self.graph if t.status == TicketStatus.FAILED)
        text = (
            f"autonomous_build complete: {green}/{total} merged_green, "
            f"{failed} failed, ${self.state.cumulative_spend_usd:.2f} spent, "
            f"waves={self.wave_order}."
        )
        return {
            "text": text,
            "stats": {
                "total_tickets": total,
                "merged_green": green,
                "failed": failed,
                "cumulative_spend_usd": self.state.cumulative_spend_usd,
            },
            "state": {
                "current_wave": self.state.current_wave,
                "ticket_status": dict(self.state.ticket_status),
                "events_tail": self.state.events[-10:],
            },
        }
