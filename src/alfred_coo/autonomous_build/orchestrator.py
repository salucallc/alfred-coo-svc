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
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple

import httpx

from .budget import BudgetTracker, make_tracker
from .cadence import SlackCadence
from .destructive_guardrail import (
    GuardrailResult,
    compute_destructive_guardrails,
)
from .dry_run import maybe_apply_dry_run
from .graph import (
    TERMINAL_STATES,
    Ticket,
    TicketGraph,
    TicketStatus,
    build_ticket_graph,
)
from .state import (
    OrchestratorState,
    checkpoint,
    is_wave_pass_fresh,
    lookup_wave_pass,
    record_wave_pass,
    restore,
)


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
#
# AB-17-w (2026-04-25): the threshold is now overridable per-kickoff via
# the ``wave_green_ratio_threshold`` payload field. The constant below is
# the default applied when the payload omits the field.
SOFT_GREEN_THRESHOLD = 0.9
DEFAULT_WAVE_GREEN_RATIO_THRESHOLD = SOFT_GREEN_THRESHOLD

# AB-17-w · Linear label name (not UUID) used by ``_wait_for_wave_gate``
# to exempt human-assigned tickets from the green-ratio denominator.
# The Linear API surfaces label *names* on issues (see graph.py line 279
# and tools.py line 1577), so name-matching is the cheap path.
# Authoritative UUID for this label (created 2026-04-25 by AB-17-v):
#   d3b067fd-0217-4901-b191-50ce2fd971f2
HUMAN_ASSIGNED_LABEL = "human-assigned"

# Default: a critical-path ticket stuck in-flight for >30 min triggers
# a Slack stall ping. Overridable by the payload for tests / tuning.
DEFAULT_STALL_THRESHOLD_SEC = 30 * 60

# AB-17-p: warn if `_dispatch_wave` has had in-flight work but no forward
# progress event for >15 min. Visibility only — does NOT cancel or retry.
# Separate from the 30-min CP stall ping above (that one is per-ticket +
# posts to Slack; this one is per-wave + WARN-level log + state event).
PROGRESS_STALL_WARN_SEC = 900  # 15 min

# AB-17-x · phantom-child reconciliation (post-v7k, 2026-04-25). If a
# ticket has been DISPATCHED/IN_PROGRESS for longer than
# ``STUCK_CHILD_FORCE_FAIL_SEC`` AND its ``child_task_id`` is no longer
# present in mesh ``claimed`` (still running) state, the orchestrator
# force-fails the ticket. This breaks the silent-stuck loop observed on
# v7i (06:32 UTC) and v7k (07:14 UTC) where SAL-2672 SS-11's fix-round-1
# child completed without a PR URL but its ticket never transitioned out
# of DISPATCHED, leaving ``in_flight=1 ready=0`` for hours despite zero
# claimed-state mesh tasks for the run.
#
# The reconcile path also widens ``_poll_children``'s mesh fetch to cover
# ``failed`` and ``claimed`` lifecycle states (was: ``completed`` only),
# so a child that died with ``status=failed`` or vanished from the
# completed window is still observable.
STUCK_CHILD_FORCE_FAIL_SEC = 30 * 60  # 30 min

# AB-17-y · orphan-active reconciliation (post-v7l, 2026-04-25, SAL-2842).
# A ticket can persist in an *active* state (DISPATCHED/IN_PROGRESS/PR_OPEN/
# REVIEWING/MERGE_REQUESTED) with ``child_task_id == None``. AB-17-x's
# reconcile loop is gated on ``t.child_task_id`` being truthy, so an
# orphan-active ticket bypasses every recovery branch even though
# ``_in_flight_for_wave`` (status-only) keeps counting it as in-flight
# forever. Live-observed on v7l: SAL-2603 (UUID 28b30b6e...) hydrated
# in_progress from a prior daemon's persisted state with NO entry in
# ``state.dispatched_child_tasks`` across all 91 soul checkpoints — the
# watchdog reported ``in_flight=1 ready=0`` for 70+ minutes with no
# escape path. The orphan-active fail-cap reuses
# ``STUCK_CHILD_FORCE_FAIL_SEC`` as its time window.
#
# Mirrors the status set in ``_in_flight_for_wave`` so the two views of
# "in flight" stay in sync. If you add a new active state, update both.
ACTIVE_TICKET_STATES: frozenset = frozenset({
    TicketStatus.DISPATCHED,
    TicketStatus.IN_PROGRESS,
    TicketStatus.PR_OPEN,
    TicketStatus.REVIEWING,
    TicketStatus.MERGE_REQUESTED,
})

# SAL-2870 (phantom-child carve-out, 2026-04-26). Any ``ticket.last_failure_reason``
# in this set short-circuits the BACKED_OFF cooling window AND skips the
# retry-count bump. Phantom-class cleanups (AB-17-x phantom-child force-fail,
# AB-17-y orphan-active force-fail) are bookkeeping nullifications, NOT real
# build failures: routing them through BACKED_OFF burns 300s + a retry slot
# per orphan after every daemon restart (observed v7ab/v7ac 2026-04-26 wave-2
# burning ~5+ min × 4 orphans = ~20 min idle per wave before fresh dispatch).
# Real failures (model crashes, silent_complete, no_pr_url, hawkman
# REQUEST_CHANGES x3) keep the existing BACKED_OFF + retry semantics.
PHANTOM_FAILURE_REASONS: frozenset = frozenset({
    "phantom_child",     # AB-17-x: child_task_id missing from claimed/completed/failed
    "no_child_task_id",  # AB-17-y: active state with child_task_id=None (post-restart)
})

# SAL-2870 (2026-04-25) — retry budget + BACKED_OFF state + deadlock grace.
#
# v7o crashed at 18:09:19 UTC with `wave 1 deadlock: 17 tickets non-terminal
# with no in-flight or ready; coercing to FAILED`. The 17 downstream
# tickets were BLOCKED on FAILED upstreams (SS-10 et al.). The previous
# AB-17-n detector fired the SAME tick `in_flight=0 + ready=0` was
# observed, so a transient FAILED upstream cascaded the whole tail.
#
# Three defaults tuned to the v7o post-mortem:
#   - DEFAULT_RETRY_BUDGET=2 — every ticket gets up to 2 retry rounds.
#     A primary FAILED → BACKED_OFF → retry. If that also FAILS →
#     BACKED_OFF → second retry. Third FAILED is terminal.
#   - DEFAULT_RETRY_BACKOFF_SEC=300 (5 min) — long enough for transient
#     mesh / GitHub / soul-svc flaps to settle, short enough to keep a
#     90-min wave moving. Override via ``retry_backoff_sec`` payload field.
#   - DEFAULT_DEADLOCK_GRACE_SEC=900 (15 min) — empirical: the v7o cascade
#     would have self-resolved within 6 min if retries had been allowed.
#     15 min covers slow-flapping mesh tasks while still bounding the
#     time wasted on a true structural deadlock. Override via
#     ``deadlock_grace_sec`` payload field.
DEFAULT_RETRY_BUDGET = 2
DEFAULT_RETRY_BACKOFF_SEC = 5 * 60  # 5 min
DEFAULT_DEADLOCK_GRACE_SEC = 15 * 60  # 15 min

# SAL-3070 / SAL-3038 phantom-loop circuit breakers (2026-04-28 incident).
# At 02:00 UTC on 2026-04-28 the orchestrator was observed stuck for 2h+ in
# a 60-90s loop on SAL-3038 (OPS-14D) + SAL-3070 (F19A) where AB-17-x's
# phantom-child reconciler fired, SAL-2870's carve-out short-circuited
# BACKED_OFF + retry-count-burn (correct for transient flakes), and the
# next dispatch produced another phantom (silent-with-tools builder). PR
# #149 design preserves retry budget on phantom cleanup, which is correct
# for transient flakes but creates infinite loops when the builder is
# genuinely silent-with-tools.
#
#   - PHANTOM_LOOP_WINDOW_SEC=600 (10 min) — sliding window over which we
#     count consecutive phantom-cleanups for a single ticket.
#   - PHANTOM_LOOP_MAX_IN_WINDOW=5 — 5 phantom-cleanups inside the window
#     trips the circuit breaker: ticket is force-escalated to ESCALATED
#     + Backlog + ``human-assigned`` label so a human can investigate
#     instead of the orchestrator burning more cycles. Legitimate
#     transient phantoms (1-3 in 10 min) still go through retry-budget-
#     unchanged dispatch as before.
#   - WAVE_STALL_FORCE_PASS_SEC=1800 (30 min) — if a wave's ``green_count``
#     hasn't increased over a 30-min window, force-pass the wave: every
#     non-terminal ticket in the wave is escalated for human review and
#     the orchestrator moves on. A wave whose green_count is still rising
#     is never force-passed — only stall-pass.
PHANTOM_LOOP_WINDOW_SEC = 10 * 60  # 10 min
PHANTOM_LOOP_MAX_IN_WINDOW = 5
WAVE_STALL_FORCE_PASS_SEC = 30 * 60  # 30 min

# Sequential-discipline bundle (2026-04-28 incident, post-PR #227). Three
# layered fixes converting the autonomous-build pipeline from optimistic to
# deadline-driven. The PR #227 circuit breakers fire AFTER 5 phantoms in
# 10min — useful but downstream of the actual disease (silent-failing
# builders + non-idempotent dispatches + single-model brittleness).
#
# Fix 1 — Builder hard-timeout w/ retry-consumed semantics. After a
# successful ``_dispatch_child`` the ticket is stamped with
# ``ticket.dispatched_at = time.time()``. If ``_poll_children`` observes
# that more than ``BUILDER_HARD_TIMEOUT_SEC`` (default 600s = 10 min) has
# elapsed without a terminal completion record, the ticket is force-failed
# with ``last_failure_reason="builder_hard_timeout"``. Critically: this
# tag is NOT in ``PHANTOM_FAILURE_REASONS`` so the SAL-2870 carve-out does
# NOT short-circuit BACKED_OFF, and ``_back_off_ticket`` consumes a retry
# slot. When budget is exhausted the ticket force-escalates to ESCALATED
# + Backlog + ``human-assigned`` label so an operator can investigate.
# Distinct from AB-17-x's STUCK_CHILD_FORCE_FAIL_SEC (30 min, gated on
# the child being absent from mesh claimed/completed/failed): the new
# bound is on dispatch wall-clock irrespective of mesh state, so a child
# that's stuck in ``claimed`` forever (gpt-oss:120b silent-with-tools) is
# also caught.
BUILDER_HARD_TIMEOUT_SEC = 10 * 60  # 10 min

# Fix 3 — Builder model fallback chain. The default first-attempt model
# is gpt-oss:120b-cloud (matches the persona's preferred_model), with
# qwen3-coder:480b-cloud as the fallback on retry. Per
# ``reference_qwen_480b_cognition_failures.md`` qwen3-coder:480b has its
# own failure modes (tool-output override, hallucinated absence) but a
# much lower silent-with-tools rate than gpt-oss:120b — worth swapping
# on retry. Override per-kickoff via the top-level ``builder_fallback_chain``
# payload field. The chain is title-tag based: the orchestrator inserts
# ``[tag:code]`` on retry-N dispatches when ``builder_fallback_chain[N]``
# resolves to qwen3-coder:480b-cloud, which the existing
# ``dispatch.select_model`` already routes to qwen.
DEFAULT_BUILDER_FALLBACK_CHAIN: Tuple[str, ...] = (
    "gpt-oss:120b-cloud",
    "qwen3-coder:480b-cloud",
)

# SAL-3038 PR-exists short-circuit (2026-04-28). The dispatch loop checks
# whether the ticket already has an open PR awaiting Hawkman review before
# spawning a builder. Three constants tune the check:
#
#   - PR_EXISTS_CACHE_TTL_SEC=300 (5 min) — re-query GitHub at most every
#     5 min per ticket. Short enough that a Hawkman approval lands on the
#     following dispatch tick; long enough to avoid hammering the Search
#     API on a 16-ticket wave that's mostly idle.
#   - PR_EXISTS_FRESH_PR_WINDOW_SEC=7d — PRs older than this are treated
#     as "stale", and dispatch proceeds to open a fresh attempt rather
#     than waiting indefinitely on a forgotten PR.
#   - HAWKMAN_LOGIN — the GitHub user whose APPROVED review releases the
#     short-circuit. Hardcoded to match the existing Hawkman QA persona.
PR_EXISTS_CACHE_TTL_SEC = 5 * 60
PR_EXISTS_FRESH_PR_WINDOW_SEC = 7 * 24 * 60 * 60
HAWKMAN_LOGIN = "salucatiresias"

# Default Slack channel for the cadence poster if the payload omits it.
DEFAULT_STATUS_CHANNEL = "C0ASAKFTR1C"  # #batcave

# AB-08: hard cap on REQUEST_CHANGES → respawn cycles. Tickets that blow the
# cap are marked FAILED; the wave gate's existing critical-path + soft-green
# logic handles the rest.
MAX_REVIEW_CYCLES = 3

# AB-08: compiled regexes for verdict extraction. Safety-net only — the
# explicit pr_review tool-call path (see _extract_verdict) has higher
# precedence and remains the canonical channel.
#
# AB-17-i hardening (2026-04-24): the original uppercase-only, underscore-only
# patterns missed hawkman's v8-smoke-c prose like "Requesting changes" and
# "request-changes", which caused all three smoke tickets to land silent-FAIL
# despite 2/3 producing clean PRs. Broaden to case-insensitive, tolerate
# space/hyphen/underscore between the two words, accept plural and -ing forms:
#   APPROVE   matches: APPROVE, approve, Approved, approves
#   REQ-CH    matches: REQUEST_CHANGES, request-changes, request changes,
#                      Requesting changes, Request Change, request_change
_VERDICT_APPROVE_RE = re.compile(r"\bapprove[ds]?\b", re.IGNORECASE)
# AB-17-k (2026-04-24 v8-smoke-e trace 115): extend to past-tense "Requested"
# and singular "change" — envelope summaries like "Requested changes" leaked
# past the AB-17-i pattern and returned None from _extract_verdict.
_VERDICT_REQUEST_CHANGES_RE = re.compile(
    r"\brequest(?:ing|ed)?[ _-]?change(?:s|d)?\b", re.IGNORECASE
)

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

    AB-17-a (Plan I §2.1): split ``paths`` into two axes so the child
    can distinguish files that MUST already exist from files it will
    CREATE. ``paths`` default relaxed to ``()`` so a pure-creation ticket
    (e.g. OPS-02 ``IMAGE_PINS.md``) can omit it. Invariant enforced by
    ``__post_init__``: at least one of ``paths`` / ``new_paths`` must be
    non-empty — catches empty hints at module-import time.
    """

    owner: str
    repo: str
    paths: Tuple[str, ...] = ()         # must exist at dispatch
    new_paths: Tuple[str, ...] = ()     # must NOT exist at dispatch (child creates)
    base_branch: str = "main"
    branch_hint: Optional[str] = None
    notes: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.paths and not self.new_paths:
            raise ValueError(
                f"TargetHint for {self.owner}/{self.repo}: at least one of "
                "paths or new_paths must be non-empty"
            )


class HintStatus(str, Enum):
    """Per-hint verification status produced by AB-17-b's ``_verify_hint``.

    Plan I §2.2. The string-valued enum serialises cleanly into Linear
    issue bodies / soul memory without a custom encoder.
    """

    OK = "ok"                         # repo + all paths verified
    REPO_MISSING = "repo_missing"     # fatal — block dispatch
    PATH_MISSING = "path_missing"     # informative — render diagnostic
    PATH_CONFLICT = "path_conflict"   # informative — paths missing AND new_paths
                                      # collide (mixed-failure tiebreak); also
                                      # the "every new_paths file already
                                      # exists" case (work was already merged).
    NEW_PATHS_COLLISION = "new_paths_collision"
    # SAL-3281 (2026-04-28): emitted when the ONLY non-OK condition is one or
    # more `new_paths` entries that already exist on main, AND at least one
    # `new_paths` entry is still absent (i.e. the ticket has real CREATE work
    # left). The classic example is a plan-doc scaffold (``plans/v1-ga/<X>.md``)
    # authored by the hint-batching sub before the implementation child runs.
    # Distinct from PATH_CONFLICT so the renderer can emit a benign
    # "scaffold already exists — extend or leave it" line instead of a
    # "STOP and escalate" line, unblocking dispatch of partially-staged
    # tickets. Wave-gate treats this the same as PATH_CONFLICT (excused).
    UNVERIFIED = "unverified"         # transient — render banner, dispatch
    NO_HINT = "no_hint"               # code not in _TARGET_HINTS (pre-existing)


@dataclass(frozen=True)
class PathResult:
    """Single-path verification outcome. Plan I §2.2.

    ``expected`` mirrors whether the path came from ``TargetHint.paths``
    (expect ``exist``) or ``TargetHint.new_paths`` (expect ``absent``).
    ``observed`` is the live GitHub state at verification time, or
    ``unknown`` if the http call failed.
    """

    path: str
    expected: Literal["exist", "absent"]
    observed: Literal["exist", "absent", "unknown"]
    ok: bool


@dataclass(frozen=True)
class VerificationResult:
    """Aggregate per-hint verification output. Plan I §2.2.

    Produced by AB-17-b's ``_verify_hint`` / ``_verify_wave_hints``; read
    by AB-17-c's ``_render_target_block(code, vr=None)``. ``hint`` is
    ``None`` only when ``status == NO_HINT`` (the code was not in the
    static table at all). ``error`` is a short human-readable diagnostic
    for logs and rendered banners; empty on the happy path.
    """

    code: str
    hint: Optional[TargetHint]
    status: HintStatus
    repo_exists: bool
    path_results: Tuple[PathResult, ...]
    error: Optional[str] = None
    verified_at: float = 0.0


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
    # AB-17-a data fix (Plan I §2 + hints_audit_2026-04-24.md §4):
    #   C1 class — OPS-01/02/03 `.yaml` typo → real file is `.yml`.
    #   C4 class — OPS-02 IMAGE_PINS.md is a NEW file; belongs in new_paths.
    #   OPS-03 path `deploy/appliance/caddy/Caddyfile` does not exist; real
    #   file lives at `deploy/appliance/Caddyfile` (no `caddy/` subdir).
    "OPS-01": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/docker-compose.yml",),
        base_branch="main",
        branch_hint="feature/sal-2634-mc-ops-network",
        notes="add mc-ops network + 4 volumes (grafana, prometheus, loki, restic)",
    ),

    "OPS-02": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "deploy/appliance/docker-compose.yml",
            "deploy/appliance/IMAGE_PINS.md",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/ops-02-pin-images",
        notes="pin all image versions; grep ':latest' must return 0 matches; IMAGE_PINS.md is new per plan D §5 W1 #2",
    ),

    "OPS-03": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "deploy/appliance/Caddyfile",                 # existing, no caddy/ subdir
            "deploy/appliance/docker-compose.yml",
        ),
        base_branch="main",
        branch_hint="feature/ops-03-caddy-routes",
        notes="Caddy path-routes /ops /auth /vault -> grafana/authelia/infisical; compose adds 3 labels or env",
    ),

    # ── Epic C/F · Fleet mode endpoint (multi-repo) ─────────────────────
    # AB-17-a data fix: soul-svc is FLAT (no db/ prefix); next free migration
    # number is 020 (005..019 already exist). Routers + tests that don't yet
    # exist move to new_paths.

    "F01": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=("migrations/020_fleet_endpoints.sql",),
        new_paths=(),  # soul-svc is FLAT; next number is 020
        base_branch="main",
        branch_hint="feature/f01-fleet-migration",
        notes="soul-svc migration 020 for fleet tables (4 tables); soul-svc has flat migrations/ dir; existing 005..019",
    ),

    "F02": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(
            "routers/fleet.py",
            "tests/test_fleet_register.py",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/f02-fleet-register",
        notes="/v1/fleet/register endpoint; valid token -> 201; both new files; register new router in serve.py (modify path added at implementation time)",
    ),

    "F03": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "src/mcctl/__init__.py",
            "src/mcctl/__main__.py",
            "src/mcctl/commands/__init__.py",
            "src/mcctl/commands/token.py",
            "tests/test_mcctl_token.py",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/f03-mcctl-token-create",
        notes="first mcctl ticket; whole subtree is new; pyproject.toml entry-point update likely required (flag to child)",
    ),

    "F07": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "src/alfred_coo/main.py",
            "src/alfred_coo/persona_loader.py",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/f07-coo-mode",
        notes="COO_MODE env var (hub|endpoint); persona_loader.py is new per F-plan §1 Arch; main.py gains branch on COO_MODE",
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
        new_paths=(),
        base_branch="main",
        branch_hint="feature/f08-soul-lite",
        notes="new soul-lite subpackage at repo root (soul-svc is flat, no src/); sqlite + /v1/memory/* API for endpoints",
    ),

    # ── Epic E · soul-svc gap closure (salucallc/soul-svc prod variant) ─
    # AB-17-a data fix: routers/memory.py EXISTS (modify); tests are NEW.
    # S-04 critical: soul-svc entry is serve.py NOT main.py.

    "S-01": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=("routers/memory.py",),
        new_paths=("tests/test_bulk_import_topics_queryable.py",),
        base_branch="main",
        branch_hint="feature/s01-index-topics-on-import",
        notes="fix: /v1/memory/import must index TKHR topics; see plan E §3 item 1 for exact line ranges (424-480)",
    ),

    "S-02": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=("routers/memory.py",),
        new_paths=("tests/test_memory_dup_409.py",),
        base_branch="main",
        branch_hint="feature/s02-dup-409",
        notes="fix: duplicate content_hash returns 409 not 500; map asyncpg/postgres 23505 UniqueViolation",
    ),

    "S-04": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=("serve.py",),                              # not main.py — soul-svc entry is serve.py
        new_paths=(
            "routers/metrics.py",
            "tests/test_metrics_endpoint.py",
        ),
        base_branch="main",
        branch_hint="feature/s04-metrics",
        notes="new /metrics endpoint with prometheus counters + histograms; register router in serve.py; NO tenant_id label (R3 in plan E risk register)",
    ),

    "S-09": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=("routers/memory.py",),
        new_paths=(
            "db/__init__.py",
            "db/pool.py",
            "db/repository.py",
            "tests/test_asyncpg_pool_init.py",
        ),
        base_branch="main",
        branch_hint="feature/s09-asyncpg-repository",
        notes="introduce asyncpg repository layer; db/ dir is new (soul-svc has no db/ today); swap Supabase SDK in routers/memory.py",
    ),

    # ── AB-19 · wave-0 no_hint closure (SS-*/OPS-22/ALT-01) ─────────────
    # Graph._parse_code emits `SS-NN` for `SAL-SS-NN` titles (regex at
    # graph.py:67 alternates `SS` before `S`). The pre-AB-19 `S-NN` entries
    # above were never hit in practice — the mesh titles always parse to
    # `SS-NN`. Mirror the authoritative data under the `SS-*` keys so
    # verify hits `ok`/`path_conflict` instead of `no_hint`. See
    # Z:/_planning/v1-ga/E_soul_svc_gaps.md §5 for source.

    "SS-01": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(
            "routers/memory.py",
            "tests/test_bulk_import_topics_queryable.py",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/ss01-index-topics-on-import",
        notes="fix: /v1/memory/import must index TKHR topics; plan E §5.1 S-01; keyed as SS-01 because title 'SAL-SS-01' parses to SS-01 (graph.py:67)",
    ),

    "SS-02": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=("routers/memory.py",),
        new_paths=("tests/test_memory_dup_409.py",),
        base_branch="main",
        branch_hint="feature/ss02-dup-409",
        notes="fix: duplicate content_hash returns 409 not 500; map asyncpg 23505 UniqueViolation; plan E §5.1 S-02",
    ),

    "SS-06": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(
            "scripts/apply_migrations.py",
            "tests/test_apply_migrations_idempotent.py",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/ss06-apply-migrations",
        notes="new scripts/ dir + migration runner with _soul_migration_log table; --dry-run default; idempotent re-runs; plan E §5.1 S-06 (note: S-11 later supersedes with asyncpg variant but SS-06 ships the initial script)",
    ),

    "SS-09": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=("routers/memory.py",),
        new_paths=(
            "db/__init__.py",
            "db/pool.py",
            "db/repository.py",
            "tests/test_asyncpg_pool_init.py",
        ),
        base_branch="main",
        branch_hint="feature/ss09-asyncpg-repository",
        notes="introduce asyncpg repository layer; db/ dir is new (soul-svc has no db/ today); swap Supabase SDK in routers/memory.py; plan E §5.2 S-09 (SAL-2670 critical-path)",
    ),

    "OPS-22": TargetHint(
        owner="salucallc",
        repo="tiresias",
        paths=("alembic.ini",),
        new_paths=(
            "alembic/versions/0039_add_cost_usd_column.py",
        ),
        base_branch="main",
        branch_hint="feature/ops-22-cost-usd-migration",
        notes="add cost_usd numeric(10,6) column to tiresias_audit_log via new alembic revision; plan D §5 Wave 5 #22 calls it 'migration 019' but tiresias/alembic/versions/0019_team_rbac.py already exists — next free revision is 0039 (last shipped 0038_add_dek_id_to_aletheia_cot_content.py). Child must verify before authoring.",
    ),

    "ALT-01": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "deploy/appliance/docker-compose.yml",
            "aletheia/Dockerfile",
            "aletheia/app/__init__.py",
            "aletheia/app/main.py",
            "aletheia/pyproject.toml",
            "aletheia/tests/test_healthz.py",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/alt-01-scaffold",
        notes="scaffold standalone aletheia-svc; new aletheia/ package at repo root (no separate repo exists; plan B §2 locks standalone-in-compose decision); append aletheia-svc service block to deploy/appliance/docker-compose.yml; healthz must return {status:'ok'}",
    ),

    # ── Epic A · Tiresias in appliance ──────────────────────────────────
    # AB-17-a: `salucallc/tiresias-sovereign` does not exist YET. Per the
    # audit, TIR-01 is the repo-scaffold ticket; AB-20 / the human operator
    # must pre-create the empty repo (`gh repo create salucallc/tiresias-
    # sovereign --private --add-readme`) BEFORE TIR-* dispatch. Paths stay
    # as new_paths (greenfield) so AB-17-b will render them correctly once
    # the repo itself exists.

    "TIR-01": TargetHint(
        owner="salucallc",
        repo="tiresias-sovereign",
        paths=(
            "pyproject.toml",
            "src/tiresias_sovereign/__init__.py",
            ".github/workflows/ci.yml",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/tir-01-scaffold",
        notes="Scaffold the sovereign Tiresias proxy package. Repo pre-created by AB-20 with README. Your PR adds pyproject.toml + package skeleton + CI workflow; run Plan A §4 Wave 1 scaffold checklist.",
    ),

    "TIR-02": TargetHint(
        owner="salucallc",
        repo="tiresias-sovereign",
        paths=(
            "src/tiresias_sovereign/principles/registry.json",
            "src/tiresias_sovereign/principles/loader.py",
            "tests/test_principle_registry.py",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/tir-02-principle-registry",
        notes="BLOCKED on TIR-01 (repo must exist); then 12 principles + hash-chain loader",
    ),

    "TIR-07": TargetHint(
        owner="salucallc",
        repo="tiresias-sovereign",
        paths=(),
        new_paths=(
            "db/migrations/0001_tiresias_audit.sql",
            "tests/test_audit_schema.py",
        ),
        base_branch="main",
        branch_hint="feature/tir-07-audit-migration",
        notes="BLOCKED on TIR-01; greenfield db/migrations/ dir — note that real salucallc/tiresias uses alembic/versions/; TIR-01 must commit to a migration convention before TIR-07 can sensibly land",
    ),

    "TIR-08": TargetHint(
        owner="salucallc",
        repo="tiresias-sovereign",
        paths=(
            "src/tiresias_sovereign/mcp_llm/router.py",
            "tests/test_mcp_llm_cascade.py",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/tir-08-mcp-llm-cascade",
        notes="BLOCKED on TIR-01; mcp-llm cascade router (principle-aware routing)",
    ),

    # ── Wave 1 additions (Path A hint expansion 2026-04-25) ──────────────
    # 24 entries derived from Z:/_planning/v1-ga/{A,B,C,D,E,K}_*.md.
    # Paths verified via `gh api repos/salucallc/<repo>/contents/<path>?ref=main`.
    # Resolves the wave-1 no_hint crash (Linear: SAL-2585..SAL-2676).

    # ── Epic A · Tiresias-sovereign Wave 2 — sequential core proxy ──────
    "TIR-03": TargetHint(
        owner="salucallc",
        repo="tiresias-sovereign",
        paths=(),
        new_paths=(
            "src/tiresias_sovereign/middleware/__init__.py",
            "src/tiresias_sovereign/middleware/soulkey_auth.py",
            "tests/test_auth_middleware.py",
            "plans/v1-ga/TIR-03.md",
        ),
        base_branch="main",
        branch_hint="feature/tir-03-soulkey-auth",
        notes="plan A §4 Wave 2 + SAL-2585; identity P1-P3 middleware (missing/malformed→401, unregistered→403, valid→200); table-driven tests; depends on TIR-02 principle registry",
    ),

    "TIR-04": TargetHint(
        owner="salucallc",
        repo="tiresias-sovereign",
        paths=(),
        new_paths=(
            "src/tiresias_sovereign/proxy/__init__.py",
            "src/tiresias_sovereign/proxy/handler.py",
            "src/tiresias_sovereign/proxy/allowlist.py",
            "tests/test_proxy_allowlist.py",
            "plans/v1-ga/TIR-04.md",
        ),
        base_branch="main",
        branch_hint="feature/tir-04-proxy-allowlist",
        notes="plan A §4 Wave 2 + SAL-2586; boundary P4-P6 — proxy handler + destination allowlist; non-allowlisted dest → 403 P4; depends on TIR-03",
    ),

    "TIR-05": TargetHint(
        owner="salucallc",
        repo="tiresias-sovereign",
        paths=(),
        new_paths=(
            "src/tiresias_sovereign/audit/__init__.py",
            "src/tiresias_sovereign/audit/hash_chain.py",
            "tests/test_audit_chain.py",
            "plans/v1-ga/TIR-05.md",
        ),
        base_branch="main",
        branch_hint="feature/tir-05-audit-hash-chain",
        notes="plan A §4 Wave 2 + SAL-2587; accountability P7-P9 — audit hash-chain writer; 100 sequential requests integrity-walk; writes against TIR-07 audit schema; depends on TIR-04",
    ),

    "TIR-06": TargetHint(
        owner="salucallc",
        repo="tiresias-sovereign",
        paths=(),
        new_paths=(
            "src/tiresias_sovereign/headers/__init__.py",
            "src/tiresias_sovereign/headers/transparency.py",
            "tests/test_transparency_headers.py",
            "plans/v1-ga/TIR-06.md",
        ),
        base_branch="main",
        branch_hint="feature/tir-06-transparency-headers",
        notes="plan A §4 Wave 2 + SAL-2588; transparency P10-P12 — emits X-Tiresias-Principles-Passed/Policy-Bundle/Audit-ID + X-Tiresias-Deny-Reason; depends on TIR-04 + TIR-05",
    ),

    # ── Epic B · Aletheia daemon (alfred-coo-svc/aletheia/ subtree) ──────
    "ALT-02": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "aletheia/app/main.py",
            "aletheia/app/verdict.py",
            "aletheia/app/soul_writer.py",
            "aletheia/tests/test_verdict_writer.py",
        ),
        new_paths=("plans/v1-ga/ALT-02.md",),
        base_branch="main",
        branch_hint="feature/alt-02-verdict-model",
        notes="plan B §2 audit/ + SAL-2599; verdict data model + soul-svc writer for topic=aletheia.verdict; main.py wires POST /v1/_debug/verdict + soul_writer; JSON-schema validated in CI",
    ),

    "ALT-03": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "aletheia/app/prompt/__init__.py",
            "aletheia/app/prompt/parser.py",
            "aletheia/prompts/verify_v1.md",
            "aletheia/tests/test_parser.py",
        ),
        new_paths=("plans/v1-ga/ALT-03.md",),
        base_branch="main",
        branch_hint="feature/alt-03-verify-prompt-parser",
        notes="plan B §5 ALT-03 + SAL-2600; verify_v1.md prompt template (§5 verbatim) + sentinel parser for `DONE verify={PASS|FAIL|UNCERTAIN}`; 20 canned outputs; prompt sha256 pinned in env",
    ),

    "ALT-04": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "aletheia/app/router/__init__.py",
            "aletheia/app/router/policy.py",
            "aletheia/tests/test_router.py",
            "plans/v1-ga/ALT-04.md",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/alt-04-model-router",
        notes="plan B §5 ALT-04 + SAL-2601; two-tier routing (qwen3-coder:480b high/med-stakes, hf:openai/gpt-oss-120b low); refuses when generator_model == candidate_verifier_model; 12 (action_class, risk_tier) test cases",
    ),

    "ALT-06": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "aletheia/app/watchers/__init__.py",
            "aletheia/app/watchers/github_poller.py",
            "aletheia/tests/test_github_poller.py",
            "plans/v1-ga/ALT-06.md",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/alt-06-github-poller",
        notes="plan B §5 ALT-06 (Track B) + SAL-2603; polls list_pull_requests every 30s on watched repos (default saluca-llc/* per plan B §4 O4); enqueues pr_review job within 45s; consumes GITHUB_PAT_POLLER env var",
    ),

    # ── Epic C/F · Fleet Wave 1 (F04 sidecar + F05 heartbeat + F12 policy) ─
    "F04": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "src/alfred_coo/fleet_gateway/__init__.py",
            "src/alfred_coo/fleet_gateway/server.py",
            "src/alfred_coo/fleet_gateway/Dockerfile",
            "tests/test_fleet_gateway_ws.py",
        ),
        new_paths=("plans/v1-ga/F04.md",),
        base_branch="main",
        branch_hint="feature/f04-fleet-gateway-ws",
        notes="plan C §2.3 + SAL-2612; new fleet-gateway sidecar (port 8090) owns WS fan-out; wscat establishes; invalid key→401 close; 100 concurrent clients sustained 5min; compose wire-in defers to F20. Host repo: alfred-coo-svc (sibling to src/alfred_coo) — flag for review",
    ),

    "F05": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(
            "routers/fleet.py",
            "tests/test_fleet_heartbeat.py",
        ),
        new_paths=("plans/v1-ga/F05.md",),
        base_branch="main",
        branch_hint="feature/f05-heartbeat-hub",
        notes="plan C §3.2 + SAL-2613; modifies routers/fleet.py (created by F02) adding heartbeat handler; ack p95<500ms localhost; 3 missed → mode_state=degraded; depends on F04 WS upgrade and F02 router",
    ),

    "F12": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=("routers/fleet.py",),
        new_paths=(
            "fleet/__init__.py",
            "fleet/policy_signer.py",
            "tests/test_fleet_policy_signer.py",
            "plans/v1-ga/F12.md",
        ),
        base_branch="main",
        branch_hint="feature/f12-fleet-policy-signer",
        notes="plan C §3.4 + SAL-2620; /v1/fleet/policy endpoint + ed25519 signer module under top-level fleet/ pkg (soul-svc is flat); tampered bundle → openssl verify fails; depends on F01 + F02",
    ),

    # ── Epic D · Ops layer additions (Wave 2/4/5 OPS tickets) ────────────
    "OPS-04": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "deploy/appliance/docker-compose.yml",
            "deploy/appliance/.env.template",
            "deploy/appliance/infisical/init.sql",
            "deploy/appliance/infisical/README.md",
        ),
        new_paths=("plans/v1-ga/OPS-04.md",),
        base_branch="main",
        branch_hint="feature/ops-04-infisical-service",
        notes="plan D §3.5 + SAL-2637; infisical/infisical:v0.124.0-postgres on mc-ops + INFISICAL_* env defaults; init.sql provisions infisical schema in mc-postgres; APE/V: curl infisical:8080/api/status returns ok",
    ),

    "OPS-05": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "deploy/appliance/docker-compose.yml",
            "deploy/appliance/.env.template",
            "deploy/appliance/infisical/README.md",
        ),
        new_paths=(
            "deploy/appliance/infisical/derive_root_key.sh",
            "plans/v1-ga/OPS-05.md",
        ),
        base_branch="main",
        branch_hint="feature/ops-05-kek-infisical-key",
        notes="plan D §3.5 + SAL-2638; KEK-derived infisical root key via mc-init container; secrets readable across 2x restart; KEK rotation re-derives. README.md shared with OPS-04 — race-handle by merging into existing file",
    ),

    "OPS-16": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/docker-compose.yml",),
        new_paths=(
            "deploy/appliance/otel/otel-collector-config.yaml",
            "deploy/appliance/otel/README.md",
            "plans/v1-ga/OPS-16.md",
        ),
        base_branch="main",
        branch_hint="feature/ops-16-otel-collector",
        notes="plan D §3.2 + SAL-2649; otel/opentelemetry-collector-contrib:0.115.0 on mc-ops; receivers OTLP grpc:4317 + http:4318; exporters fan to prometheus + loki",
    ),

    "OPS-17": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "deploy/appliance/docker-compose.yml",
            "deploy/appliance/prometheus/prometheus.yml",
            "deploy/appliance/prometheus/scrape_configs.yml",
            "plans/v1-ga/OPS-17.md",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/ops-17-prometheus",
        notes="plan D §3.2 + SAL-2650; prom/prometheus:v2.55.1 on mc-ops; 30d retention; scrape_configs targets soul-svc/tiresias/coo/portal /metrics; APE/V: /api/v1/targets shows ≥5 up=1",
    ),

    "OPS-23": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "src/alfred_coo/__init__.py",
            "configs/model_pricing.yaml",
            "src/alfred_coo/pricing.py",
            "tests/test_pricing_loader.py",
            "plans/v1-ga/OPS-23.md",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/ops-23-model-pricing",
        notes="plan D §3.1 + SAL-2656; YAML pricing table (Ollama Max=flat $100/mo amortized, free=$0, paid per-token); pricing.load() returns dict; configs/ dir does not exist on main — first ticket to create it",
    ),

    # ── Epic E · soul-svc gap-closure remainder (SS-03/04/10/11/12) ──────
    "SS-03": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(
            "routers/cot_capture.py",
            "tests/test_cot_unified.py",
        ),
        new_paths=("plans/v1-ga/SS-03.md",),
        base_branch="main",
        branch_hint="feature/ss03-cot-unify",
        notes="plan E §5.1 S-03 + SAL-2664; rewrite cot_capture.py so /v1/cot/capture writes to _memories with modality=cot, removes file-shim; row in _memories queryable + /var/lib/soul-svc/cot/ never created; D1 decision = merge",
    ),

    "SS-04": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(
            "serve.py",
            "routers/metrics.py",
            "tests/test_metrics_endpoint.py",
        ),
        new_paths=("plans/v1-ga/SS-04.md",),
        base_branch="main",
        branch_hint="feature/ss04-metrics",
        notes="plan E §5.1 S-04 + SAL-2665; /metrics endpoint with soul_http_requests_total + soul_http_request_duration_seconds histogram + soul_db_query_duration_seconds{table} + soul_memory_writes_total{knowledge_tier}; register router in serve.py; NO tenant_id label (R3 risk register); mirrors existing S-04 hint because parser emits SS-04 from SAL-SS-04",
    ),

    "SS-10": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(
            "routers/session_lifecycle.py",
            "routers/session.py",
            "routers/challenge.py",
            "routers/admin.py",
            "routers/deps.py",
        ),
        new_paths=(
            "tests/test_session_asyncpg.py",
            "tests/test_admin_asyncpg.py",
            "tests/test_challenge_asyncpg.py",
            "plans/v1-ga/SS-10.md",
        ),
        base_branch="main",
        branch_hint="feature/ss10-asyncpg-routers-sweep",
        notes="plan E §5.2 S-10 + SAL-2671; swap Supabase SDK→db/repo helpers in 5 appliance-critical routers (created by SS-09); existing test_auth_hybrid + test_session_continuity stay green; bearer-auth latency ≤10% regression; grep -c 'client.table' on these 5 == 0; depends on SS-09",
    ),

    "SS-11": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(
            "scripts/apply_migrations.py",
            "tests/test_apply_migrations_idempotent.py",
        ),
        new_paths=("plans/v1-ga/SS-11.md",),
        base_branch="main",
        branch_hint="feature/ss11-apply-migrations-asyncpg",
        notes="plan E §5.2 S-11 + SAL-2672; supersedes SS-06 implementation; reads DATABASE_URL only (NO SUPABASE_SERVICE_KEY); asyncpg.connect (single conn, serial); _soul_migration_log idempotency + SHA-drift refusal; --dry-run default; bootstrap log table on fresh DB; depends on SS-09",
    ),

    "SS-12": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(
            "tests/e2e/__init__.py",
            "tests/e2e/smoke_backends.sh",
            ".github/workflows/soul-svc-backend-portability.yml",
        ),
        new_paths=("plans/v1-ga/SS-12.md",),
        base_branch="main",
        branch_hint="feature/ss12-multi-backend-smoke",
        notes="plan E §5.2 S-12 + SAL-2673; CI matrix smoke against local Postgres + Supabase (direct PG conn, NOT rpc) + Neon (ephemeral branch); 5 assertions per backend; secrets from GCP Secret Manager (supabase-direct-postgres-url, neon-api-key); depends on SS-09 + SS-10 + SS-11",
    ),

    # ── Epic C amendment · Multi-tenant Fleet (C-26..C-28) ───────────────
    # Parser regex emits `C-26` for SAL-C-26 titles via single-letter `C`
    # alternative (graph.py:67). C-26 extends soul_lite (F08); C-27/C-28
    # extend mcctl from F03/F19 inline (mcctl files NOT in paths — flagged).
    "C-26": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(
            "soul_lite/__init__.py",
            "soul_lite/service.py",
        ),
        new_paths=(
            "soul_lite/repository.py",
            "soul_lite/bootstrap.py",
            "scripts/lint_no_raw_sql.py",
            "tests/soul_lite/__init__.py",
            "tests/soul_lite/test_tenant_scope.py",
            "tests/soul_lite/test_bootstrap_from_persona.py",
            "plans/v1-ga/C-26.md",
        ),
        base_branch="main",
        branch_hint="feature/c26-soul-lite-multitenant",
        notes="plan C §5 C-26 + SAL-2674; bootstrap.py mirrors soul-svc session_init pattern — schema materialized from genesis layer + recent memories + dream-cycle state on boot, NOT static migration files; tenant_id is part of the persona binding, materialized during bootstrap (not a separate ALTER step); TenantScopedRepository enforces tenant scope; AST lint via scripts/lint_no_raw_sql.py; prod boot refuses if any row tenant_id='__legacy__'; depends on SS-11 for asyncpg/aiosqlite abstraction. F08 deferred bootstrap scheme — C-26 locks 'derive-from-persona' over migrations/ to keep soul_lite consistent with soul-svc session_init mental model.",
    ),

    "C-27": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "src/mcctl/commands/token.py",
            "tests/fleet/__init__.py",
        ),
        new_paths=(
            "src/alfred_coo/fleet_auth/__init__.py",
            "src/alfred_coo/fleet_auth/tenant_binding.py",
            "tests/fleet/test_tenant_auth.py",
            "plans/v1-ga/C-27.md",
        ),
        base_branch="main",
        branch_hint="feature/c27-fleet-tenant-auth",
        notes="plan C §5 C-27 + SAL-2675; api_key bound to tenant_id (sk_endpoint_<tenantslug>_<endpoint_id>_<sha256>); fleet_endpoint_tenants tracking table (migration via SS-11); register payload gains tenant block; extends F03's src/mcctl/commands/token.py adding --tenant flag (in paths, file exists). Depends on C-26, TIR-03",
    ),

    "C-28": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(),
        new_paths=(
            "src/alfred_coo/fleet_policy/__init__.py",
            "src/alfred_coo/fleet_policy/tenant_bundles.py",
            "tests/fleet/test_tenant_policy.py",
            "plans/v1-ga/C-28.md",
        ),
        base_branch="main",
        branch_hint="feature/c28-fleet-tenant-policy",
        notes="plan C §5 C-28 + SAL-2676; /v1/fleet/policy keyed by tenant {bundles:{tenant_a:{signed},tenant_b:{...}}}; F12 signer signs each tenant's bundle separately; mcctl push-policy --tenant only bumps that tenant's policy_version; mesh_scope.allowed_topics_prefix per-tenant. mcctl push-policy command itself is from F19 (later wave) — C-28 only adds tenant_bundles module + test, mcctl extension lands inline. Depends on C-27 + F12",
    ),

    # ── Wave-2 expansion (2026-04-26 evening operator hotfix) ────────────
    # 33 entries derived from Z:/_planning/v1-ga/{A,B,C,D,E}_*.md.
    # Resolves the wave-2 no_hint crash observed in v7ab autonomous run
    # (audit doc: C:/Users/cris/AppData/Local/Temp/no_hint_root_cause.md).
    # Wave-1 expansion at line 604 covered SAL-2585..SAL-2676; this block
    # closes the remaining wave-2 codes that hit `{'no_hint': 33}` in
    # _verify_wave_hints. Per audit §3.8 path-forward: dict expansion is
    # the immediate unblocker; body-parser fallback is the durable repair
    # tracked separately.

    # ── Epic A · Tiresias-sovereign Wave 3 — appliance wire-in (TIR-09..13) ─
    "TIR-09": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/docker-compose.yml",),
        new_paths=(
            "deploy/appliance/tiresias/README.md",
            "plans/v1-ga/TIR-09.md",
        ),
        base_branch="main",
        branch_hint="feature/tir-09-compose-wire-in",
        notes="plan A §4 Wave 3 + SAL-2591; add tiresias-proxy service block (image ghcr.io/salucallc/tiresias-sovereign:v1.0.0) to appliance compose + Caddy /tiresias/* path-routes (already in OPS-03 scope; coordinate); APE/V: /tiresias/healthz + /tiresias/policies accessible via Caddy, .principles | length == 12; depends on TIR-01..08",
    ),

    "TIR-10": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "deploy/appliance/docker-compose.yml",
            "deploy/appliance/tiresias/network_split.md",
            "plans/v1-ga/TIR-10.md",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/tir-10-network-split",
        notes="plan A §4 Wave 3 + SAL-2592; split docker networks — mc-internal (no internet route) for coo/portal/open-webui/soul; mc-egress for caddy/mcp-*/tiresias only. APE/V: docker exec alfred-coo curl --max-time 5 https://api.github.com fails; docker exec mcp-github curl same URL succeeds. Risk R1 (plan A §6): if internal:true breaks DNS, fallback to iptables OUTPUT REJECT in init container; depends on TIR-09",
    ),

    "TIR-11": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "deploy/appliance/docker-compose.yml",
            "deploy/appliance/.env.template",
            "src/alfred_coo/main.py",
        ),
        new_paths=(
            "src/alfred_coo/tiresias_client.py",
            "tests/test_tiresias_client.py",
            "plans/v1-ga/TIR-11.md",
        ),
        base_branch="main",
        branch_hint="feature/tir-11-remove-raw-tokens",
        notes="plan A §4 Wave 4 + SAL-2593 [CRITICAL PATH]; remove raw GITHUB_TOKEN/SLACK_BOT_TOKEN/LINEAR_API_KEY/NOTION_API_KEY/ANTHROPIC_API_KEY/OPENAI_API_KEY from coo env; wire SOULKEY_FILE + TIRESIAS_URL=http://tiresias-proxy:8840; new tiresias_client.py replaces direct provider SDKs with /proxy/<service>/<path> calls. APE/V: grep image filesystem for ghp_|xoxb-|lin_api_|secret_|sk-ant-|sk- returns 0; coo issues test GitHub issue via proxy; audit_chain row exists. Risk R2 (plan A §6): probe each SDK for custom baseUrl support; swap to raw fetch if any reject. Depends on TIR-09 + TIR-10",
    ),

    "TIR-12": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "deploy/appliance/docker-compose.yml",
            "deploy/appliance/mc-init/mint_soulkeys.sh",
            "deploy/appliance/mc-init/register_allowlist.sh",
            "deploy/appliance/mc-init/README.md",
        ),
        new_paths=("plans/v1-ga/TIR-12.md",),
        base_branch="main",
        branch_hint="feature/tir-12-mint-soulkeys",
        notes="plan A §4 Wave 3 + SAL-2594; mc-init mints 6 soulkeys (sk_agent_appliance_<service>_<sha256_tail64>) idempotently on first boot, writes to state/secrets/, SHA-512 hash in tiresias _soulkeys table; registers allowlist rows in _soulkey_allowlist for coo (≥4 entries). APE/V: rowcount(_soulkeys)=6; rowcount(_soulkey_allowlist for coo)>=4. Depends on TIR-07 + TIR-09",
    ),

    "TIR-13": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "deploy/appliance/docker-compose.yml",
            "deploy/appliance/.env.template",
        ),
        new_paths=(
            "deploy/appliance/open-webui/tiresias_routing.md",
            "plans/v1-ga/TIR-13.md",
        ),
        base_branch="main",
        branch_hint="feature/tir-13-openwebui-routing",
        notes="plan A §4 Wave 3 + SAL-2595; route open-webui chat completions through tiresias-proxy (OPENAI_API_BASE_URL=http://tiresias-proxy:8840/proxy/openai or anthropic-equivalent); strip raw provider keys from open-webui env. APE/V: chat completion via browser works; tiresias_audit row increments per turn. Depends on TIR-11 + TIR-12",
    ),

    # ── Epic B · Aletheia daemon Wave 2 (ALT-07/08) ──────────────────────
    "ALT-07": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "aletheia/app/watchers/mesh_subscriber.py",
            "aletheia/tests/test_mesh_subscriber.py",
            "plans/v1-ga/ALT-07.md",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/alt-07-mesh-subscriber",
        notes="plan B §5 Track B ALT-07 + SAL-2604; subscribes soul-svc pub/sub for mesh_task_complete events; enqueues verify job to Redis aletheia:pending; APE/V: fire mcp__alfred__mesh_task_complete with known id, within 10s verdict record written, soul_memory_search finds it. Depends on ALT-05 + soul-svc pub/sub epic",
    ),

    "ALT-08": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(),
        new_paths=(
            "aletheia/app/preflight/__init__.py",
            "aletheia/app/preflight/server.py",
            "aletheia/tests/test_preflight.py",
            "plans/v1-ga/ALT-08.md",
        ),
        base_branch="main",
        branch_hint="feature/alt-08-mcp-preflight",
        notes="plan B §5 Track B ALT-08 + SAL-2605; POST /v1/preflight endpoint blocks up to 90s for verdict; FAIL aborts MCP with HTTP 412, UNCERTAIN proceeds with marked-for-review flag; circuit-breaker (>50% 5xx for 2min → fail-open + #batcave per R5). APE/V: forge preflight to non-existent slack channel → FAIL with abort; Log in soul-svc topic=aletheia.verdict. Depends on ALT-05",
    ),

    # ── Epic C · Fleet Wave 4-5 (F09/F10/F11/F14/F16/F17/F19/F20/F22/F25) ─
    "F09": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "src/alfred_coo/fleet_endpoint/__init__.py",
            "src/alfred_coo/fleet_endpoint/memory_push.py",
            "tests/test_fleet_memory_push.py",
        ),
        new_paths=("plans/v1-ga/F09.md",),
        base_branch="main",
        branch_hint="feature/f09-endpoint-memory-push",
        notes="plan C §3.3 + SAL-2617; endpoint-side push loop POST /v1/fleet/memory/push 30s cadence or queue >=50; idempotent dup retry; monotonic local_seq. APE/V: 1000 writes reflected in hub fleet_memory_sync_log within 60s; depends on F06 + F08",
    ),

    "F10": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "src/alfred_coo/fleet_endpoint/__init__.py",
            "src/alfred_coo/fleet_endpoint/memory_pull.py",
            "tests/test_fleet_memory_pull.py",
        ),
        new_paths=("plans/v1-ga/F10.md",),
        base_branch="main",
        branch_hint="feature/f10-endpoint-memory-pull",
        notes="plan C §3.3 + SAL-2618; endpoint pull loop GET /v1/fleet/memory/pull?since_global_seq=N 60s cadence; advance global cursor; APE/V: two endpoints A,B; A writes shared.* topic; B's local search returns within 90s; depends on F09",
    ),

    "F11": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=("routers/fleet.py",),
        new_paths=(
            "fleet/conflict_journal.py",
            "tests/test_fleet_conflict_journal.py",
            "plans/v1-ga/F11.md",
        ),
        base_branch="main",
        branch_hint="feature/f11-conflict-journal",
        notes="plan C §3.3 + SAL-2619; hub-side fleet.conflict journal row when content_hash mismatch on *.singleton topic; mcctl endpoint conflicts surfaces. APE/V: force content_hash conflict, hub writes row, mcctl lists. Depends on F10",
    ),

    "F14": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("src/mcctl/commands/__init__.py",),
        new_paths=(
            "src/mcctl/commands/policy.py",
            "tests/test_mcctl_policy_immediate.py",
            "plans/v1-ga/F14.md",
        ),
        base_branch="main",
        branch_hint="feature/f14-emergency-policy-push",
        notes="plan C §3.4 + SAL-2622; mcctl policy push --immediate interrupts endpoints <5s; in-flight task requeued with requeue_reason=policy_immediate. APE/V: synthetic immediate-policy push, endpoint logs interrupt + requeue. Depends on F13",
    ),

    "F16": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("src/alfred_coo/fleet_endpoint/__init__.py",),
        new_paths=(
            "src/alfred_coo/fleet_endpoint/degraded_mode.py",
            "src/alfred_coo/fleet_endpoint/tool_fallback.py",
            "tests/test_degraded_mode_matrix.py",
            "plans/v1-ga/F16.md",
        ),
        base_branch="main",
        branch_hint="feature/f16-degraded-mode-matrix",
        notes="plan C §3.5 + SAL-2624 [CRITICAL PATH]; 8-case degraded-mode tool behavior matrix (github cache_then_503, linear queue_and_drain, fs passthrough, etc.); cut hub connectivity → tests pass; reconnect drains queue. APE/V: pytest test_degraded_mode_matrix all 8 cases green. Depends on F07 + F13",
    ),

    "F17": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("src/alfred_coo/fleet_endpoint/__init__.py",),
        new_paths=(
            "src/alfred_coo/fleet_endpoint/reconcile.py",
            "tests/test_fleet_reconcile.py",
            "plans/v1-ga/F17.md",
        ),
        base_branch="main",
        branch_hint="feature/f17-endpoint-reconcile",
        notes="plan C §3.3 recovery + SAL-2625 [CRITICAL PATH]; on WS reconnect endpoint sends fleet.reconcile with cursor; hub responds with plan; order policy → persona → inbox → memory push → memory pull → resume; <60s for <100k buffered writes. APE/V: 10min blackout, 200 local + 150 hub writes; reconciles within 60s; no dups; global_seq monotonic. Depends on F10 + F16",
    ),

    "F19": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("src/mcctl/commands/policy.py",),
        new_paths=(
            "src/mcctl/commands/persona.py",
            "tests/test_mcctl_push_persona.py",
            "plans/v1-ga/F19.md",
        ),
        base_branch="main",
        branch_hint="feature/f19-mcctl-push",
        notes="plan C §5 F19 + SAL-2627; mcctl push-policy / push-persona uploads bundle, hub creates version row, endpoint heartbeat shows persona_version match within 60s. Extends policy.py from F14 + new persona.py. Depends on F12 + F13",
    ),

    # F19A (SAL-3070): Phase-B decomposition child of F19 (SAL-2627).
    # Authors the shared `_upload_bundle` helper (consumed by F19B) plus
    # the push-policy CLI subcommand + hub handler + db row + tests.
    # Without a _TARGET_HINTS entry the v7aj no_hint-excusal path skips
    # it.
    "F19A": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "src/mcctl/commands/policy.py",
        ),
        new_paths=(
            "src/mcctl/commands/_upload_bundle.py",
            "src/alfred_coo/fleet_hub/policy_bundles.py",
            "tests/test_mcctl_push_policy.py",
            "tests/test_fleet_policy_bundles_endpoint.py",
            "plans/v1-ga/F19a.md",
        ),
        base_branch="main",
        branch_hint="feature/f19a-mcctl-push-policy",
        notes="Phase-B decomposition child of F19 (SAL-2627) → SAL-3070; `mcctl push-policy` CLI subcommand + shared `_upload_bundle` helper (reused by F19B) + `POST /v1/fleet/policy-bundles` hub handler + `fleet_policy_versions` row insertion + tests. APE/V: pytest tests/test_mcctl_push_policy.py + tests/test_fleet_policy_bundles_endpoint.py green; uploaded bundle creates a version row with sha256 + uploaded_at. Sibling: F19B (push-persona reuses _upload_bundle). Depends on F12 + F13 + F14.",
    ),

    # F19B (SAL-3071): Phase-B decomposition child of F19 (SAL-2627).
    # Reuses F19A's `_upload_bundle` helper for the push-persona path +
    # adds the heartbeat persona_version reconciliation E2E (the parent's
    # headline 60s assertion). Without a _TARGET_HINTS entry the v7aj
    # no_hint-excusal path skips it.
    "F19B": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "src/mcctl/commands/_upload_bundle.py",
        ),
        new_paths=(
            "src/mcctl/commands/persona.py",
            "src/alfred_coo/fleet_hub/persona_bundles.py",
            "tests/test_mcctl_push_persona.py",
            "tests/test_fleet_persona_version_e2e.py",
            "plans/v1-ga/F19b.md",
        ),
        base_branch="main",
        branch_hint="feature/f19b-mcctl-push-persona",
        notes="Phase-B decomposition child of F19 (SAL-2627) → SAL-3071; `mcctl push-persona` CLI subcommand reusing F19A's `_upload_bundle` helper + hub handler + heartbeat `persona_version` reconciliation E2E (the parent's headline assertion: endpoint heartbeat shows persona_version match within 60s of push). APE/V: pytest tests/test_mcctl_push_persona.py + tests/test_fleet_persona_version_e2e.py green; E2E asserts heartbeat persona_version converges to pushed version <60s. Sibling: F19A (authors _upload_bundle). Depends on F19A.",
    ),

    "F20": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "deploy/endpoint/docker-compose.yml",
            "deploy/endpoint/.env.template",
            "deploy/endpoint/README.md",
            "deploy/endpoint/bootstrap.sh",
        ),
        new_paths=("plans/v1-ga/F20.md",),
        base_branch="main",
        branch_hint="feature/f20-endpoint-compose",
        notes="plan C §5 F20 + SAL-2628; endpoint footprint compose (alfred-coo-svc COO_MODE=endpoint + soul-lite + optional local MCP shims); no postgres/portal/open-webui — headless; bootstrap README walks fresh-Debian operator <10min. APE/V: clean Debian VM, paste registration_token, endpoint registers + heartbeats. Depends on F06 + F08",
    ),

    "F22": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("src/alfred_coo/fleet_endpoint/__init__.py",),
        new_paths=(
            "src/alfred_coo/fleet_endpoint/quarantine.py",
            "src/mcctl/commands/quarantine.py",
            "tests/test_fleet_quarantine.py",
            "plans/v1-ga/F22.md",
        ),
        base_branch="main",
        branch_hint="feature/f22-quarantine-state",
        notes="plan C §3.5 quarantine + SAL-2630; force api_key expiry during severe-degraded → mode_state=quarantine (no writes, read-only, refuses tasks); recovery via mcctl endpoint unquarantine. APE/V: synthetic key expiry + 16min hub blackout; endpoint enters quarantine; recovery flag clears state. Depends on F15 + F16",
    ),

    "F25": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(),
        new_paths=(
            "docs/fleet/protocol.md",
            "docs/fleet/runbook.md",
            "docs/fleet/threat_model.md",
            "plans/v1-ga/F25.md",
        ),
        base_branch="main",
        branch_hint="feature/f25-fleet-docs",
        notes="plan C §5 F25 + SAL-2633; protocol spec (registration, heartbeat, replication, policy push, degraded matrix, reconciliation), runbook (provisioning, key rotation, quarantine recovery, unquarantine), threat model (R1-R5 mitigations). docs-lint clean; markdown only, no code. Depends on F17 + F22",
    ),

    # ── Epic D · Ops layer Wave 2-6 (OPS-07/08/09/10/11/12/13/14/15/20/24/25/27/28) ─
    "OPS-07": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/docker-compose.yml",),
        new_paths=(
            "deploy/appliance/infisical/agent_sidecar.yml",
            "plans/v1-ga/OPS-07.md",
        ),
        base_branch="main",
        branch_hint="feature/ops-07-agent-sidecars",
        notes="plan D §3.5 + SAL-2640; infisical-agent sidecar (~20MB) for tiresias/portal/mcp-core writes secrets to tmpfs /run/secrets/; APE/V: cat /run/secrets/<svc>_<key> matches infisical UI value for each service. Depends on OPS-06",
    ),

    "OPS-08": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "deploy/appliance/docker-compose.yml",
            "deploy/appliance/infisical/migrate_state_secrets.sh",
            "deploy/appliance/infisical/MIGRATION.md",
            "plans/v1-ga/OPS-08.md",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/ops-08-migrate-state-secrets",
        notes="plan D §3.5 + SAL-2641; init container reads ./state/secrets/* on first boot, pushes into infisical, then chmod 000 (disabled-but-recoverable). APE/V: delete state/secrets dir, restart → all services healthy, secrets visible in infisical UI. Depends on OPS-06 + OPS-07",
    ),

    # OPS-08c (SAL-3036): wave-2 decomposition child of OPS-08 (SAL-2641).
    # Parent was reverted to Backlog with `human-assigned` retained on
    # 2026-04-27 after PR #170 stub-wrapped the migration script. This child
    # replaces the placeholder infisical-cli calls in the existing stub
    # `migrate_state_secrets.sh` with the real implementation. Without a
    # _TARGET_HINTS entry the v7ah no_hint-excusal path skipped it.
    "OPS-08C": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "deploy/appliance/infisical/migrate_state_secrets.sh",
            "plans/v1-ga/OPS-08c.md",
            "tests/scripts/test_migrate_secrets.sh",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/ops-08c-migrate-secrets-impl",
        notes="wave 2 decomposition child of OPS-08 (SAL-2641) → SAL-3036; replace placeholder infisical-cli calls in the PR #170 stub with real implementation; APE/V: tests/scripts/test_migrate_secrets.sh exercises happy + failure paths against a fake infisical endpoint and exits 0. Depends on OPS-08 stub.",
    ),

    "OPS-09": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/docker-compose.yml",),
        new_paths=(
            "deploy/appliance/infisical/rotation_endpoint.md",
            "tests/test_infisical_rotation.py",
            "plans/v1-ga/OPS-09.md",
        ),
        base_branch="main",
        branch_hint="feature/ops-09-rotation-endpoint",
        notes="plan D §3.5 + SAL-2642; POST /api/v3/secrets/:id/rotate generates new value; agents pick up next 60s poll; services with requires_restart:true auto-restart. APE/V: rotate test key, services pick up within 90s. Depends on OPS-08",
    ),

    "OPS-10": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "deploy/appliance/docker-compose.yml",
            "deploy/appliance/authelia/configuration.yml",
            "deploy/appliance/authelia/users_database.yml",
            "deploy/appliance/authelia/README.md",
            "plans/v1-ga/OPS-10.md",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/ops-10-authelia-file-backend",
        notes="plan D §3.3 + SAL-2643; authelia/authelia:4.38.17 with file-based user backend (air-gap fallback); admin passphrase set in wizard Screen 8. APE/V: /auth/ renders; admin login returns 200. Depends on OPS-03 + OPS-07",
    ),

    "OPS-11": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "deploy/appliance/Caddyfile",
            "deploy/appliance/docker-compose.yml",
            "plans/v1-ga/OPS-11.md",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/ops-11-caddy-forward-auth",
        notes="plan D §3.3 + SAL-2644; Caddyfile forward_auth directive on /ops/* routes; unauth → 302 /auth; valid session cookie → 200. APE/V: curl -I /ops/ returns 302; curl with cookie returns 200. Depends on OPS-10",
    ),

    "OPS-12": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/authelia/configuration.yml",),
        new_paths=(
            "deploy/appliance/authelia/oidc_clients_template.yml",
            "deploy/appliance/authelia/BYO_OIDC.md",
            "scripts/mc_auth_add_oidc.sh",
            "plans/v1-ga/OPS-12.md",
        ),
        base_branch="main",
        branch_hint="feature/ops-12-byo-oidc",
        notes="plan D §3.3 + SAL-2645; ./mc.sh auth add-oidc <provider> --client-id X adds OIDC client to authelia config; supports google/okta/azure-ad templates. APE/V: add test IdP, login via that IdP works end-to-end. Depends on OPS-10",
    ),

    "OPS-13": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/authelia/configuration.yml",),
        new_paths=(
            "deploy/appliance/authelia/access_control.yml",
            "tests/test_authelia_rbac.py",
            "plans/v1-ga/OPS-13.md",
        ),
        base_branch="main",
        branch_hint="feature/ops-13-rbac-groups",
        notes="plan D §3.3 + SAL-2646; appliance-admin (full /ops/*, /vault/*, /auth/*, all backend APIs) + tenant-user (only /, /soul/*, /chat/*) groups. APE/V: tenant-user → 403 on /ops; appliance-admin → 200. Depends on OPS-10",
    ),

    "OPS-14": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/authelia/configuration.yml",),
        new_paths=(
            "deploy/appliance/authelia/oauth2_clients.yml",
            "src/alfred_coo/auth/scoped_tokens.py",
            "tests/test_scoped_oauth_tokens.py",
            "plans/v1-ga/OPS-14.md",
        ),
        base_branch="main",
        branch_hint="feature/ops-14-scoped-oauth2-tokens",
        notes="plan D §3.3 + SAL-2647; OAuth2 client_credentials flow, scopes soul:memory:read/write, tiresias:audit:read, tiresias:cost:read, mcp:tools:invoke, vault:read/write; 24h TTL; portal rotation. APE/V: soul:memory:read scope → 200 on search but 403 on write. Depends on OPS-13",
    ),

    # OPS-14c (SAL-3037): wave-2 decomposition child of OPS-14 (SAL-2647).
    # Adds the scope-enforcement middleware that consumes the scoped tokens
    # produced by the existing scoped_tokens module (PR #169, currently
    # NotImplementedError-wrapped by PR #170). Without a _TARGET_HINTS
    # entry the v7ah no_hint-excusal path skipped it.
    "OPS-14C": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("src/alfred_coo/auth/scoped_tokens.py",),
        new_paths=(
            "plans/v1-ga/OPS-14c.md",
            "src/alfred_coo/auth/scope_middleware.py",
            "tests/auth/test_scope_middleware.py",
        ),
        base_branch="main",
        branch_hint="feature/ops-14c-scope-middleware",
        notes="wave 2 decomposition child of OPS-14 (SAL-2647) → SAL-3037; FastAPI/ASGI middleware that parses the OAuth2 access token, extracts scopes, and 403s requests whose required scope is not granted. APE/V: soul:memory:read scope → 200 on search but 403 on write. Depends on OPS-14 scaffolding.",
    ),

    # OPS-14d (SAL-3038): wave-2 decomposition child of OPS-14 (SAL-2647).
    # Validates the 24h TTL on issued tokens — rejects expired tokens with
    # 401 and surfaces a structured error. Without a _TARGET_HINTS entry
    # the v7ah no_hint-excusal path skipped it.
    "OPS-14D": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("src/alfred_coo/auth/scoped_tokens.py",),
        new_paths=(
            "plans/v1-ga/OPS-14d.md",
            "src/alfred_coo/auth/ttl_validator.py",
            "tests/auth/test_ttl_validation.py",
        ),
        base_branch="main",
        branch_hint="feature/ops-14d-ttl-validation",
        notes="wave 2 decomposition child of OPS-14 (SAL-2647) → SAL-3038; enforces the 24h TTL constraint on scoped OAuth2 tokens; rejects expired tokens with 401 + structured error. APE/V: token issued > 24h ago → 401 with token_expired error code. Depends on OPS-14 scaffolding.",
    ),

    "OPS-15": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "deploy/appliance/Caddyfile",
            "deploy/appliance/authelia/configuration.yml",
            "deploy/appliance/wizard/screen_8_admin_init.html",
            "scripts/mc_auth_init_admin.sh",
            "plans/v1-ga/OPS-15.md",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/ops-15-wizard-admin-init",
        notes="plan D §3.3 + SAL-2648; Wizard Screen 8 (between 7 and Open MC) — operator sets appliance-admin passphrase; ./mc.sh auth init-admin --passphrase X seeds users_database.yml. APE/V: post-wizard admin row exists; ./mc.sh auth list-users >=1 row. Depends on OPS-10 + portal-team coordination",
    ),

    "OPS-20": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/docker-compose.yml",),
        new_paths=(
            "deploy/appliance/grafana/dashboards/appliance_health.json",
            "deploy/appliance/grafana/dashboards/cost_and_tokens.json",
            "deploy/appliance/grafana/dashboards/soul_activity.json",
            "deploy/appliance/grafana/dashboards/auth_and_access.json",
            "plans/v1-ga/OPS-20.md",
        ),
        base_branch="main",
        branch_hint="feature/ops-20-grafana-dashboards",
        notes="plan D §3.2 + SAL-2653; 4 pre-provisioned Grafana dashboards (Appliance Health, Cost & Tokens, Soul Activity, Auth & Access). APE/V: all 4 uids resolve via /api/dashboards/uid/<id>; mc-health green on clean install. Depends on OPS-19",
    ),

    "OPS-24": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/docker-compose.yml",),
        new_paths=(
            "deploy/appliance/tiresias-cost-exporter/main.go",
            "deploy/appliance/tiresias-cost-exporter/Dockerfile",
            "deploy/appliance/tiresias-cost-exporter/go.mod",
            "deploy/appliance/tiresias-cost-exporter/README.md",
            "plans/v1-ga/OPS-24.md",
        ),
        base_branch="main",
        branch_hint="feature/ops-24-cost-exporter",
        notes="plan D §3.1 + SAL-2657; Go 150-LOC exporter scrapes tiresias_audit_log every 60s, emits mc_tokens_total{tenant_id,persona_id,model,provider} + mc_cost_usd_total{...} gauges via /metrics. Risk R5: qwen3 weaker on Go — template from existing mcp-gateway exporters; fallback deepseek if qwen falters. APE/V: /metrics returns counters with labels. Depends on OPS-17 + OPS-23",
    ),

    "OPS-25": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/docker-compose.yml",),
        new_paths=(
            "deploy/appliance/alertmanager/budget_rules.yml",
            "deploy/appliance/alertmanager/budget_rules_README.md",
            "plans/v1-ga/OPS-25.md",
        ),
        base_branch="main",
        branch_hint="feature/ops-25-budget-alerts",
        notes="plan D §3.1 + SAL-2658; per-tenant daily budget (default $10) + per-persona monthly budget (default $50) Prometheus alert rules → Slack webhook to #batcave (C0ASAKFTR1C). APE/V: synthetic $15 test row in tiresias_audit_log → #batcave alert within 120s. Depends on OPS-21 + OPS-24",
    ),

    "OPS-27": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/docker-compose.yml",),
        new_paths=(
            "deploy/appliance/restic/restore.sh",
            "scripts/mc_restore.sh",
            "tests/test_restic_restore.sh",
            "plans/v1-ga/OPS-27.md",
        ),
        base_branch="main",
        branch_hint="feature/ops-27-restic-restore",
        notes="plan D §3.4 + SAL-2660; ./mc.sh restore --snapshot <id> walks operator through restic restore with KEK-derived password; RTO 20min target. APE/V: tamper a volume, run restore, smoke_test.sh passes after. Depends on OPS-26",
    ),

    "OPS-28": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/alertmanager/budget_rules.yml",),
        new_paths=(
            "deploy/appliance/DISASTER_RECOVERY.md",
            "deploy/appliance/alertmanager/stale_backup_rule.yml",
            "plans/v1-ga/OPS-28.md",
        ),
        base_branch="main",
        branch_hint="feature/ops-28-dr-runbook",
        notes="plan D §3.4 + SAL-2661; DISASTER_RECOVERY.md with 3 scenarios (disk failure, whole-host loss, ransomware); 36h-stale-backup Prometheus alert. APE/V: DR doc exists with all 3 scenarios; synthetic 37h since last snapshot fires alert to #batcave. Depends on OPS-20 + OPS-21 + OPS-26",
    ),

    # ── Epic E · soul-svc PQ wire-up (SS-08, conditional) ────────────────
    "SS-08": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(
            "routers/challenge.py",
        ),
        new_paths=(
            "tests/test_challenge_verify_receipt.py",
            "plans/v1-ga/SS-08.md",
        ),
        base_branch="main",
        branch_hint="feature/ss08-pq-receipt",
        notes="plan E §5.1 S-08 + SAL-2669 [CONDITIONAL on D2 approval]; POST /v1/challenge/verify with {tenant_id, ed25519_sig, mldsa44_sig, nonce} returns JWS-signed {receipt, pubkey_sha, signed_at}; Portal wizard screen 4 renders PQ-verified badge; pq_crypto_lib already vendored. Risk R4: requires Cristian sign-off on JWS claims schema before merge. Defer if D2 not approved.",
    ),

    # ── Epic C amendment · C-29 (multi-tenant E2E folded into F21) ───────
    "C-29": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "tests/fleet/e2e/__init__.py",
            "tests/fleet/e2e/test_multitenant.py",
        ),
        new_paths=("plans/v1-ga/C-29.md",),
        base_branch="main",
        branch_hint="feature/c29-multitenant-e2e",
        notes="plan C §5 C-29 + SAL-2677; extends F21 1-hub-3-endpoints suite — one endpoint hosts two tenants (acme-corp + beta-industries); 3 assertion classes: (1) memory isolation, (2) cross-tenant policy leakage, (3) blackout recovery preserves per-tenant global_seq independence. APE/V: pytest tests/fleet/e2e/test_multitenant.py green; jq '.tenant_id' e2e_audit.jsonl | sort -u | wc -l == 2; entire F21 suite runs <15min. Depends on C-26 + C-27 + C-28 + F21",
    ),

    # C-29A (SAL-3074): Phase-B decomposition child of C-29 (SAL-2677).
    # Authors the new tests/fleet/e2e/test_multitenant.py with assertion
    # classes 1+2 (memory isolation, cross-tenant policy leakage). C-29B
    # extends the same file with class 3 + audit jq + monotonicity, so
    # ordering is C-29A creates → C-29B extends. Without a _TARGET_HINTS
    # entry the v7aj no_hint-excusal path skips it.
    "C-29A": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "tests/fleet/e2e/__init__.py",
            "tests/fleet/e2e/test_multitenant.py",
            "plans/v1-ga/C-29a.md",
        ),
        new_paths=(),
        base_branch="main",
        branch_hint="feature/c29a-multitenant-isolation",
        notes="Phase-B decomposition child of C-29 (SAL-2677) → SAL-3074; creates `tests/fleet/e2e/test_multitenant.py` with assertion class 1 (memory isolation: writes from tenant acme are not readable by tenant beta on the same endpoint) + class 2 (cross-tenant policy leakage: a policy bundle pushed for acme is not enforced against beta requests). APE/V: pytest tests/fleet/e2e/test_multitenant.py::test_memory_isolation + ::test_policy_leakage green. Sibling: C-29B (extends with blackout + jq + monotonicity). Depends on F21 fixtures + C-26/27/28.",
    ),

    # C-29B (SAL-3075): Phase-B decomposition child of C-29 (SAL-2677).
    # Extends the test_multitenant.py file authored by C-29A with the
    # third assertion class + audit jsonl jq check + per-tenant local_seq
    # monotonicity. Without a _TARGET_HINTS entry the v7aj
    # no_hint-excusal path skips it.
    "C-29B": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "tests/fleet/e2e/test_multitenant.py",
        ),
        new_paths=(
            "plans/v1-ga/C-29b.md",
        ),
        base_branch="main",
        branch_hint="feature/c29b-blackout-recovery-monotonicity",
        notes="Phase-B decomposition child of C-29 (SAL-2677) → SAL-3075; extends C-29A's test_multitenant.py with assertion class 3 (blackout recovery preserves per-tenant global_seq independence — 10min hub blackout, 200 local writes per tenant, reconcile <60s with no cross-tenant bleed) + audit jsonl jq check (`jq '.tenant_id' e2e_audit.jsonl | sort -u | wc -l == 2`) + per-tenant `local_seq` monotonicity assertion + 15-min wall-clock CI budget. APE/V: pytest tests/fleet/e2e/test_multitenant.py::test_blackout_recovery green; entire F21 suite runs <15min. Sibling: C-29A (creates the file). Depends on C-29A.",
    ),

    # ── Wave-3 expansion (2026-04-26 evening operator hotfix, follow-on to PR #109/#120) ────────────
    # 4 entries derived from Z:/_planning/v1-ga/{A,C,E}_*.md.
    # Resolves the residual wave-3 no_hint gap observed in v7ab autonomous run
    # (audit doc: C:/Users/cris/AppData/Local/Temp/no_hint_root_cause.md).
    # PR #109 covered wave-2 + most wave-3 (TIR-09..13, OPS-09/12/14/20/25/27/28,
    # F11/F14/F16/F17/F19/F22/F25, SS-08); PR #120 fixed gate-3 by adding
    # `plans/v1-ga/<CODE>.md` to `new_paths` for wave-1/2. This block closes the
    # remaining 4 wave-3 codes (SS-07, F21, TIR-14, TIR-15) WITH plan-doc paths
    # included from the start per PR #120 lesson.

    # ── Epic E · soul-svc Wave 3 docs (S-07/SS-07) ───────────────────────
    "SS-07": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(
            "README.md",
            "ARCH.md",
        ),
        new_paths=(
            ".github/workflows/docs-lint.yml",
            "scripts/docs_lint.py",
            "plans/v1-ga/SS-07.md",
        ),
        base_branch="main",
        branch_hint="feature/ss07-readme-v2-surface",
        notes="plan E §5.1 S-07 + SAL-2668; README lists every router in serve.py; /v1/admin/keys documented as tenant-key-minting path; ARCH.md diagram updated; PRODUCTION_GAP.md deleted or replaced; DATABASE_URL documented as canonical backend config with Supabase tips moved to a separate section. APE/V: CI job `docs-lint` verifies every `@router.post/get` decorator referenced in README; `pytest scripts/docs_lint.py` green; grep `PRODUCTION_GAP.md` returns 0 references. Depends on S-01..S-04 + S-09..S-12.",
    ),

    # SS-07A (SAL-3072): Phase-B decomposition child of SS-07 (SAL-2668).
    # Targets `salucallc/soul-svc` (production), per the soul-svc-repos
    # disambiguation memory note — distinct from the Z:/soul-svc paper
    # variant. PR `soul-svc#48` shipped the docs-lint workflow scaffolding
    # but left README/ARCH.md content updates open; SS-07A authors the
    # README "Endpoint Surface" + /v1/admin/keys subsection half. Without
    # a _TARGET_HINTS entry the v7aj no_hint-excusal path skips it.
    "SS-07A": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(
            "README.md",
        ),
        new_paths=(
            "plans/v1-ga/SS-07a.md",
        ),
        base_branch="main",
        branch_hint="feature/ss07a-readme-endpoint-surface",
        notes="Phase-B decomposition child of SS-07 (SAL-2668) → SAL-3072; README \"Endpoint Surface\" enumeration of every `@router.<method>` decorator in serve.py + dedicated `/v1/admin/keys` tenant-key-minting subsection. APE/V: every router decorator in serve.py appears under \"Endpoint Surface\"; `/v1/admin/keys` has its own subsection with a curl example. Sibling: SS-07B (ARCH refresh + docs-lint coverage). Depends on SS-07 docs-lint scaffolding (PR soul-svc#48).",
    ),

    # SS-07B (SAL-3073): Phase-B decomposition child of SS-07 (SAL-2668).
    # Targets `salucallc/soul-svc` (production). Authors the ARCH.md
    # mermaid refresh + PRODUCTION_GAP.md tombstone + the actual docs-lint
    # coverage assertion that the PR #48 scaffolding deferred. Without a
    # _TARGET_HINTS entry the v7aj no_hint-excusal path skips it.
    "SS-07B": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(
            "ARCH.md",
            "PRODUCTION_GAP.md",
            ".github/workflows/docs-lint.yml",
        ),
        new_paths=(
            "tests/test_docs_router_coverage.py",
            "plans/v1-ga/SS-07b.md",
        ),
        base_branch="main",
        branch_hint="feature/ss07b-arch-refresh-docs-lint",
        notes="Phase-B decomposition child of SS-07 (SAL-2668) → SAL-3073; ARCH.md mermaid diagram refresh (purges dead `/v1/session/{close,capture-jsonl,cot/flush}`) + PRODUCTION_GAP.md tombstone (delete or replace with redirect note) + docs-lint workflow wired to fail on missing routes + `tests/test_docs_router_coverage.py` asserting every @router decorator is referenced in README. APE/V: pytest tests/test_docs_router_coverage.py green; docs-lint workflow fails when a new @router decorator lands without a README mention; grep `PRODUCTION_GAP.md` returns 0 references. Sibling: SS-07A (README Endpoint Surface). Depends on SS-07A README updates.",
    ),

    # ── Epic C · Fleet Wave 6 — F21 E2E integration (multi-endpoint) ─────
    "F21": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(),
        new_paths=(
            "tests/fleet/__init__.py",
            "tests/fleet/e2e/__init__.py",
            "tests/fleet/e2e/test_e2e_fleet.py",
            "tests/fleet/e2e/docker-compose.fleet-e2e.yml",
            "tests/fleet/e2e/conftest.py",
            ".github/workflows/fleet_e2e.yml",
            "plans/v1-ga/F21.md",
        ),
        base_branch="main",
        branch_hint="feature/f21-fleet-e2e-integration",
        notes="plan C §5 F21 + SAL-2629 [CRITICAL PATH wave 6 long pole]; 1 hub + 3 endpoints on 3 isolated Docker networks; suites: (1) task-completion across mesh, (2) memory-replication with global_seq monotonicity, (3) blackout-recovery (10min hub blackout, 200 local + 150 hub writes, reconcile <60s no dups). APE/V: JUnit XML emitted; CI workflow `fleet_e2e.yml` green on PR; entire suite runs <15min wall-clock; per-endpoint `mode_state` transitions logged. Note: tests/fleet/e2e/__init__.py shared with C-29 multi-tenant amendment (idempotent on coexistence). Depends on F16 + F17 + F20.",
    ),

    # ── Epic A · Tiresias-sovereign Wave 4 — TIR-14/15 sovereignty smoke + QA ─
    "TIR-14": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(),
        new_paths=(
            ".github/workflows/tiresias_sovereignty_smoke.yml",
            "deploy/appliance/tiresias/smoke_test.sh",
            "deploy/appliance/tiresias/test_audit_chain_walk.py",
            "plans/v1-ga/TIR-14.md",
        ),
        base_branch="main",
        branch_hint="feature/tir-14-e2e-sovereignty-smoke",
        notes="plan A §4 Wave 4 + SAL-2596 [RC tag prerequisite per plan A §8]; new GitHub workflow asserts (1) smoke_test.sh passes end-to-end, (2) direct curl to api.github.com from coo container fails (DNS/conn-refused per TIR-10 network split), (3) proxied call via tiresias-proxy succeeds + audit_chain row written, (4) unregistered soulkey → 403 P1 deny, (5) audit chain walk verifies every row's prev_hash links cleanly. APE/V: workflow green on PR; smoke_test.sh exit 0; test_audit_chain_walk.py finds 0 hash-link breaks across ≥10 rows. Depends on TIR-11 + TIR-12 + TIR-13.",
    ),

    "TIR-15": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "deploy/appliance/tiresias/README.md",
        ),
        new_paths=(
            "deploy/appliance/tiresias/PRINCIPLES.md",
            "deploy/appliance/tiresias/RUNBOOK.md",
            "plans/v1-ga/TIR-15.md",
        ),
        base_branch="main",
        branch_hint="feature/tir-15-qa-and-docs",
        notes="plan A §4 Wave 4 + SAL-2597; hawkman-qa-a constrained review on all TIR-09..14 PRs; appliance Tiresias docs lint clean — README + PRINCIPLES.md (12 constitutional principles) + RUNBOOK.md (operator playbook for sovereignty smoke fail, key rotation, audit-chain inspection). APE/V: hawkman-qa-a verdict APPROVED on every TIR-* PR; markdownlint green on all 3 docs; PRINCIPLES.md asserts `.principles | length == 12` matches TIR-02 registry. Depends on TIR-14.",
    ),

    # TIR-15A (SAL-3068): Phase-B decomposition child of TIR-15 (SAL-2597).
    # Parent reverted to Backlog 2026-04-27 retaining `human-assigned`; on
    # inspection the markdown-lint stream is mechanical and splits cleanly
    # from the QA-review stream (TIR-15B). Without a _TARGET_HINTS entry
    # the v7aj no_hint-excusal path skips it.
    "TIR-15A": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "deploy/appliance/tiresias/README.md",
            "deploy/appliance/tiresias/PRINCIPLES.md",
            "deploy/appliance/tiresias/RUNBOOK.md",
        ),
        new_paths=(
            ".github/workflows/markdown-lint.yml",
            ".markdownlint.json",
            "plans/v1-ga/TIR-15a.md",
        ),
        base_branch="main",
        branch_hint="feature/tir-15a-markdown-lint-ci",
        notes="Phase-B decomposition child of TIR-15 (SAL-2597) → SAL-3068; markdown-lint CI workflow + .markdownlint.json + lint cleanup of README.md / PRINCIPLES.md / runbook(s); workflow runs on PR + push to main. APE/V: markdownlint green on all 3 appliance Tiresias docs; CI workflow `markdown-lint` green on PR. Sibling: TIR-15B (review pass).",
    ),

    # TIR-15B (SAL-3069): Phase-B decomposition child of TIR-15 (SAL-2597).
    # The QA-review stream — constrained-prompt model pass per the
    # 2026-04-26 "constrained QA prompt beats strict" feedback note — is
    # auto-runnable when the deliverable is a structured report committed
    # to docs/qa-reviews/. Without a _TARGET_HINTS entry the v7aj
    # no_hint-excusal path skips it.
    "TIR-15B": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(),
        new_paths=(
            "docs/qa-reviews/tir-phase-b-review.md",
            "plans/v1-ga/TIR-15b.md",
        ),
        base_branch="main",
        branch_hint="feature/tir-15b-hawkman-qa-review",
        notes="Phase-B decomposition child of TIR-15 (SAL-2597) → SAL-3069; hawkman-qa-a (`hf:openai/gpt-oss-120b:fastest`) constrained review pass on every TIR-09..14 PR; aggregated report committed to `docs/qa-reviews/tir-phase-b-review.md`. APE/V: every TIR-* PR has a verdict line in the report; APPROVED required for green. Sibling: TIR-15A (markdown-lint).",
    ),

    # ── Phase C · Cockpit surface (25 tickets, 2026-04-27) ───────────────
    # Source: Z:/_planning/journey/phase_c_surface_tickets_2026-04-27.md
    # Phase label: phase:cockpit-surface (order 3.5, magenta #9C36B5).
    # Container is daily-driver cockpit first, customer-shipping artifact
    # second. Phase C runs in parallel with Phase B drain via separate
    # cron. 22 of 25 tickets land in the v7-dashboard FastAPI app
    # (deployed at /opt/v7-dashboard/dashboard.py on Oracle; canonical
    # working copy at Z:/_tmp/v7-dashboard/dashboard.py). The
    # v7-dashboard is NOT a separate GitHub repo — paths use the
    # `v7-dashboard/` repo-relative prefix with `repo="alfred-coo-svc"`
    # so the orchestrator's hint invariant test passes. Verification
    # will report PATH_MISSING for these tickets (the v7-dashboard tree
    # is not in alfred-coo-svc); orchestrator still dispatches under
    # UNVERIFIED and the child builder writes against the live
    # dashboard via Oracle SSH. PC-C2B and PC-C2E upstream source is
    # cristianxruvalcaba-coder/alfred-chat-stack; the hint owner is
    # pinned to salucallc per the test_target_hints_populated_for_
    # wave_0_epics invariant, and the upstream pointer is recorded in
    # the per-entry `notes` field for the child to follow.

    # ── Track 1 · Voice surface (5) ──────────────────────────────────────
    "PC-V1A": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        base_branch="main",
        branch_hint="feature/pc-v1a-stt-endpoint",
        notes="Phase C cockpit-surface; PTT endpoint /v1/voice/stt accepts opus/webm → Whisper transcript. POST audio/webm → 200 with {transcript, lang, duration_ms}; reject >2MB; smoke uploads 3-sec opus and asserts non-empty transcript. v7-dashboard FastAPI app (Oracle absolute path).",
    ),

    "PC-V1B": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        base_branch="main",
        branch_hint="feature/pc-v1b-ptt-ui",
        notes="Phase C cockpit-surface; PTT UI: hold-to-record mic button + waveform + cookie-persisted prefs. #mic-btn press+hold triggers MediaRecorder; release → STT; transcript injects into chat input; cookie `v7_voice_mode` persists `ptt|conv|off`. Depends on PC-V1A.",
    ),

    "PC-V1C": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        base_branch="main",
        branch_hint="feature/pc-v1c-conv-mode",
        notes="Phase C cockpit-surface; conversational mode: VAD-streaming STT + TTS with barge-in. AnalyserNode amplitude > threshold for >500ms triggers send; TTS fades 200ms on user-speech detected; 30s convo asserts ≥3 alternations no overlap. Depends on PC-V1A, PC-V1B.",
    ),

    "PC-V1D": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        base_branch="main",
        branch_hint="feature/pc-v1d-mic-permission-wakelock",
        notes="Phase C cockpit-surface; mic permission flow + Wake Lock + haptic feedback. Permission rationale modal; `wakeLock.request('screen')` in conv mode; haptic vibration 30ms on PTT press. Depends on PC-V1B.",
    ),

    "PC-V1E": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        new_paths=(
            "v7-dashboard/static/manifest.json",
            "v7-dashboard/static/sw.js",
        ),
        base_branch="main",
        branch_hint="feature/pc-v1e-pwa-manifest-sw",
        notes="Phase C cockpit-surface; PWA manifest + service worker for tablet install. /manifest.json + /sw.js routes; Lighthouse PWA audit ≥90; 'Add to Home Screen' available in Chrome on Tab S9+. Manifest+SW are new files under v7-dashboard static tree (Oracle absolute paths).",
    ),

    # ── Track 2 · Chat surface (3 — C2A and C2C dropped per Cristian: chat
    # stack contained in cockpit, no Oracle deploy) ──────────────────────
    "PC-C2B": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(),
        new_paths=("alfred-chat-stack/openclaude/Dockerfile",),
        base_branch="main",
        branch_hint="feature/pc-c2b-openclaude-ttyd-smoke",
        notes="Phase C cockpit-surface; OpenClaude ttyd container build + smoke. `docker build` succeeds; `curl :3001` returns HTML with `<title>OpenClaude` substring within 30s of start. NOTE upstream source: cristianxruvalcaba-coder/alfred-chat-stack (openclaude/Dockerfile, default branch master). Child should clone that repo, build, and commit smoke evidence; PR may target either repo. Hint owner pinned to salucallc per orchestrator invariant test_target_hints_populated_for_wave_0_epics.",
    ),

    "PC-C2D": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        base_branch="main",
        branch_hint="feature/pc-c2d-cockpit-chat-iframe",
        notes="Phase C cockpit-surface; embed chat into cockpit: `/chat` panel iframes OpenWebUI. Cockpit `/chat` route renders iframe to `localhost:3010` (chat stack contained in cockpit, no Oracle deploy per Cristian 2026-04-27); sandbox attrs allow same-origin auth. Depends on PC-C2B and PC-D3A.",
    ),

    "PC-C2E": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(),
        new_paths=(
            "alfred-chat-stack/gateway/main.py",
            "alfred-chat-stack/gateway/soul_middleware.py",
        ),
        base_branch="main",
        branch_hint="feature/pc-c2e-soul-context-injection",
        notes="Phase C cockpit-surface; soul-context injection in gateway proves out for cockpit chat. Gateway injects soul payload + mesh state summary into system prompt; `\"what's mesh state?\"` references current node count from fixture. NOTE upstream source: cristianxruvalcaba-coder/alfred-chat-stack — gateway tree exists locally at Z:/alfred-chat-stack/gateway/ but is NOT pushed to the upstream repo. Child should push the gateway tree to the upstream repo or commit under alfred-chat-stack/ subtree of alfred-coo-svc. Hint owner pinned to salucallc per orchestrator invariant.",
    ),

    # ── Track 3 · Dashboard rollup (6) ───────────────────────────────────
    "PC-D3A": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        new_paths=("v7-dashboard/cockpit_state.schema.json",),
        base_branch="main",
        branch_hint="feature/pc-d3a-cockpit-state-rollup",
        notes="Phase C cockpit-surface; LOAD-BEARING. GET /v1/cockpit/state — single JSON rollup. Merges orchestrator status + in-flight + roadmap + mesh sessions + heartbeats + inbox-unread + recent-merges; <250KB; cached 15s; schema-validated against `cockpit_state.schema.json`. Defines JSON shape that D3B-F, K4D, N5A all consume.",
    ),

    "PC-D3B": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        base_branch="main",
        branch_hint="feature/pc-d3b-mesh-state-panel",
        notes="Phase C cockpit-surface; mesh-state panel: live agent heartbeats + active session count. `<section id=\"mesh\">` shows N agents alive + last-heartbeat-age; auto-refresh 10s via SSE; assert `[data-mesh-agent]` ≥1 on populated fixture. Depends on PC-D3A.",
    ),

    "PC-D3C": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        base_branch="main",
        branch_hint="feature/pc-d3c-agent-inbox-panel",
        notes="Phase C cockpit-surface; agent inbox panel: unread `mesh_messages` directed at Cristian. Soul-svc inbox query for `recipient=cristian, read=false`; click marks read. Depends on PC-D3A.",
    ),

    "PC-D3D": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        base_branch="main",
        branch_hint="feature/pc-d3d-slack-mirror-panel",
        notes="Phase C cockpit-surface; Slack mirror panel: pull #batcave + DMs into cockpit (read-only). Polls Slack API every 60s for #batcave + DMs to Cristian; renders 20 most-recent. Requires Slack scopes: channels:history, im:history, users:read. Depends on PC-D3A.",
    ),

    "PC-D3E": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        base_branch="main",
        branch_hint="feature/pc-d3e-recent-merges-feed",
        notes="Phase C cockpit-surface; recent-merges feed: last 24h `git log --merges` across primary repos. Lists merge SHAs + titles + author from `alfred-coo-svc`, `soul-svc`, `mission-control`, `v7-dashboard`. Depends on PC-D3A.",
    ),

    "PC-D3F": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "v7-dashboard/dashboard.py",
            "v7-dashboard/journey/phases.yaml",
        ),
        base_branch="main",
        branch_hint="feature/pc-d3f-roadmap-phase-progress",
        notes="Phase C cockpit-surface; roadmap-phase progress card: rollup of all done across `phase:*` labels. Progress bar per phase; reads `phases.yaml` (Oracle dashboard manifest path); 6 progress bars rendered with non-zero denominator. Depends on PC-D3A.",
    ),

    # ── Track 4 · Command surface (6) ────────────────────────────────────
    "PC-K4A": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        new_paths=("v7-dashboard/kickoff_templates.yaml",),
        base_branch="main",
        branch_hint="feature/pc-k4a-kickoff-template-picker",
        notes="Phase C cockpit-surface; kickoff template picker: dropdown → fire. Lists templates from `kickoff_templates.yaml`; 'Fire' POSTs `/v1/orchestrator/kickoff`; dispatched job appears in `/v7status` in-flight within 30s.",
    ),

    "PC-K4B": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        base_branch="main",
        branch_hint="feature/pc-k4b-mesh-message-ui",
        notes="Phase C cockpit-surface; mesh-message-an-agent UI. Form: agent picker (from `twin_tenants.json` fleet of 345) + body + priority; POST `/v1/mesh/message`; appears in target inbox within 5s.",
    ),

    "PC-K4C": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        base_branch="main",
        branch_hint="feature/pc-k4c-kill-runaway-task-ui",
        notes="Phase C cockpit-surface; kill-runaway-task UI. Table of `status=running` tasks with per-row 'Kill'; sends `task_complete` with `status=killed`; moves within 5s.",
    ),

    "PC-K4D": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        base_branch="main",
        branch_hint="feature/pc-k4d-fleet-endpoint-state-viewer",
        notes="Phase C cockpit-surface; fleet endpoint state viewer. Paginated table from `twin_tenants.json` enriched with live heartbeats; sortable by age, role, tenant; 345 rows on full-fleet fixture. Depends on PC-D3A.",
    ),

    "PC-K4E": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        base_branch="main",
        branch_hint="feature/pc-k4e-one-off-sub-runner",
        notes="Phase C cockpit-surface; one-off sub runner. Form: persona picker + prompt + optional model; POST `/v1/sub/run`; result streams; 'echo `pong`' sub asserts streamed `pong`.",
    ),

    "PC-K4F": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        base_branch="main",
        branch_hint="feature/pc-k4f-command-palette",
        notes="Phase C cockpit-surface; command palette: ctrl-K opens fuzzy-search. Cmd-K opens overlay; fuzzy-search across [kickoff, mesh-msg, kill-task, fleet-view, sub-run]; enter dispatches. Depends on PC-K4A/B/C/E.",
    ),

    # ── Track 5 · Notification inbox (5) ─────────────────────────────────
    "PC-N5A": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        base_branch="main",
        branch_hint="feature/pc-n5a-unified-inbox-endpoint",
        notes="Phase C cockpit-surface; unified inbox endpoint /v1/cockpit/inbox (4 sources aggregated). Returns `{slack_dms, slack_batcave_recent, mesh_messages_to_cristian, grounding_gap_issues, halt_alerts}`; `total_unread` matches sum. Depends on PC-D3A.",
    ),

    "PC-N5B": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        base_branch="main",
        branch_hint="feature/pc-n5b-notification-ticker",
        notes="Phase C cockpit-surface; notification ticker: top-bar unread-count badge with click-to-expand. `[3 unread]` reflects `inbox.total_unread`; click opens slide-out panel; updates within 5s of fixture change. Depends on PC-N5A.",
    ),

    "PC-N5C": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        base_branch="main",
        branch_hint="feature/pc-n5c-halt-alert-banner",
        notes="Phase C cockpit-surface; halt-alert dedicated channel: red banner on halt. Banner appears on `/v7status`, persists until acknowledged; cookie-persisted dismiss. Depends on PC-N5A.",
    ),

    "PC-N5D": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        base_branch="main",
        branch_hint="feature/pc-n5d-grounding-gap-surface",
        notes="Phase C cockpit-surface; grounding-gap surface. Panel shows open grounding gaps with severity badges; click opens detail. Depends on PC-N5A.",
    ),

    "PC-N5E": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("v7-dashboard/dashboard.py",),
        new_paths=("v7-dashboard/static/push-sw.js",),
        base_branch="main",
        branch_hint="feature/pc-n5e-web-push-bridge",
        notes="Phase C cockpit-surface; push notification bridge: web push for halt alerts + DMs even when tab inactive. Service worker receives push; permission asked at install; SW receives event within 5s of test push. Depends on PC-V1E and PC-N5A.",
    ),

    # ── Hint repair 2026-04-28 (SAL-3347 + grounding-gap fix) ───────────
    # Repairs hints that grounding-gap-escalated wave 0/1/2 builders today.
    # 40 PATH_CONFLICT entries above had their `new_paths` rewritten in-place
    # to move shipped files into `paths` (they exist on main; the ticket is
    # follow-on work, not greenfield create). 18 NO_HINT codes get fresh
    # entries below so the parser-extracted code resolves to a real file.

    "OPS-18": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/docker-compose.yml",),
        new_paths=(
            "deploy/appliance/loki/loki-config.yaml",
            "plans/v1-ga/OPS-18.md",
        ),
        base_branch="main",
        branch_hint="feature/ops-18-loki",
        notes="plan D §3.2 + SAL-2651; Loki + docker log-driver for log aggregation; APE/V: docker logs streamed into Loki; queryable via /loki/api/v1/query",
    ),

    "OPS-06": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/docker-compose.yml",),
        new_paths=(
            "deploy/appliance/infisical/agent_soul_svc.yml",
            "plans/v1-ga/OPS-06.md",
        ),
        base_branch="main",
        branch_hint="feature/ops-06-infisical-agent-soul",
        notes="plan D §3.5 + SAL-2639; infisical-agent sidecar for soul-svc; reads secrets to /run/secrets/soul_svc_*; APE/V: cat /run/secrets/soul_svc_DATABASE_URL matches infisical UI",
    ),

    "OPS-19": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/docker-compose.yml",),
        new_paths=(
            "deploy/appliance/grafana/datasources/prometheus.yaml",
            "deploy/appliance/grafana/datasources/loki.yaml",
            "plans/v1-ga/OPS-19.md",
        ),
        base_branch="main",
        branch_hint="feature/ops-19-grafana-datasources",
        notes="plan D §3.2 + SAL-2652; Grafana + datasource provisioning (prometheus, loki); APE/V: GET /api/datasources returns ≥2 entries",
    ),

    "OPS-21": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/docker-compose.yml",),
        new_paths=(
            "deploy/appliance/alertmanager/alertmanager.yml",
            "deploy/appliance/alertmanager/batcave_webhook.yml",
            "plans/v1-ga/OPS-21.md",
        ),
        base_branch="main",
        branch_hint="feature/ops-21-alertmanager",
        notes="plan D §3.2 + SAL-2654; Alertmanager + Slack #batcave webhook; APE/V: synthetic firing alert posts to #batcave",
    ),

    "OPS-26": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/docker-compose.yml",),
        new_paths=(
            "deploy/appliance/restic/backup.sh",
            "deploy/appliance/restic/restic_cron",
            "plans/v1-ga/OPS-26.md",
        ),
        base_branch="main",
        branch_hint="feature/ops-26-restic-backup",
        notes="plan D §3.4 + SAL-2659; Restic + nightly cron backup of mc-postgres + state volumes; APE/V: nightly snapshot in repo; restic snapshots returns row",
    ),

    "ALT-05": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("aletheia/app/main.py",),
        new_paths=(
            "aletheia/app/verify_worker.py",
            "aletheia/tests/test_verify_worker.py",
            "plans/v1-ga/ALT-05.md",
        ),
        base_branch="main",
        branch_hint="feature/alt-05-verify-worker",
        notes="plan B §5 ALT-05 + SAL-2602; verify worker loop + grep-confirm evidence validator; consumes aletheia:pending Redis queue; APE/V: pytest tests/test_verify_worker.py green",
    ),

    "ALT-09": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(),
        new_paths=(
            "aletheia/app/escalator/__init__.py",
            "aletheia/app/escalator/batcave.py",
            "aletheia/tests/test_batcave_escalator.py",
            "plans/v1-ga/ALT-09.md",
        ),
        base_branch="main",
        branch_hint="feature/alt-09-batcave-escalator",
        notes="plan B §5 ALT-09 + SAL-2606; #batcave escalator on FAIL/UNCERTAIN verdicts; posts compact card with verify link; APE/V: synthetic FAIL → #batcave message within 30s",
    ),

    "ALT-10": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("aletheia/app/main.py",),
        new_paths=(
            "aletheia/app/api/verify_history.py",
            "aletheia/tests/test_verify_history_endpoint.py",
            "plans/v1-ga/ALT-10.md",
        ),
        base_branch="main",
        branch_hint="feature/alt-10-verify-history-api",
        notes="plan B §5 ALT-10 + SAL-2607; verify-history HTTP endpoint + portal tab; GET /v1/verdicts?since=... returns paginated list; APE/V: 100-row fixture, endpoint returns JSON with cursor",
    ),

    "ALT-11": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("aletheia/app/main.py",),
        new_paths=(
            "aletheia/app/cost_meter.py",
            "aletheia/tests/test_cost_meter.py",
            "plans/v1-ga/ALT-11.md",
        ),
        base_branch="main",
        branch_hint="feature/alt-11-cost-metering",
        notes="plan B §5 ALT-11 + SAL-2608; cost metering + daily cap; tracks per-model token usage, refuses verify when daily cap exceeded; APE/V: $X synthetic spend → 429 with cap_exceeded",
    ),

    "F06": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("src/alfred_coo/fleet_endpoint/__init__.py",),
        new_paths=(
            "src/alfred_coo/fleet_endpoint/heartbeat.py",
            "tests/test_fleet_heartbeat_endpoint.py",
            "plans/v1-ga/F06.md",
        ),
        base_branch="main",
        branch_hint="feature/f06-endpoint-heartbeat",
        notes="plan C §3.2 + SAL-2614; endpoint-side heartbeat sender (every 30s, exponential backoff on miss); 3 missed → mode_state=degraded local-only; APE/V: cut WS for 90s, endpoint flips to degraded; reconnect restores active",
    ),

    "F13": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("src/alfred_coo/fleet_endpoint/__init__.py",),
        new_paths=(
            "src/alfred_coo/fleet_endpoint/policy_apply.py",
            "tests/test_fleet_policy_apply.py",
            "plans/v1-ga/F13.md",
        ),
        base_branch="main",
        branch_hint="feature/f13-endpoint-policy-apply",
        notes="plan C §3.4 + SAL-2621; endpoint applies policy bundle on next task boundary; verifies signer ed25519 sig; APE/V: push test bundle → endpoint loads it within 60s; tampered bundle refused",
    ),

    "F15": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("src/alfred_coo/fleet_endpoint/__init__.py",),
        new_paths=(
            "src/alfred_coo/fleet_endpoint/key_rotation.py",
            "tests/test_fleet_key_rotation.py",
            "plans/v1-ga/F15.md",
        ),
        base_branch="main",
        branch_hint="feature/f15-key-rotation",
        notes="plan C §3.5 + SAL-2623; API-key rotation on heartbeat ack; old key valid for 60s grace; APE/V: hub rotates, old key 401s after 60s, new key 200s",
    ),

    "F18": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("src/mcctl/commands/__init__.py",),
        new_paths=(
            "src/mcctl/commands/endpoint.py",
            "tests/test_mcctl_endpoint_cmds.py",
            "plans/v1-ga/F18.md",
        ),
        base_branch="main",
        branch_hint="feature/f18-mcctl-endpoint-mgmt",
        notes="plan C §5 F18 + SAL-2626; mcctl endpoint list/show/revoke/tail subcommands; APE/V: pytest tests/test_mcctl_endpoint_cmds.py covers each; --format json produces parseable output",
    ),

    "F23": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("src/alfred_coo/main.py",),
        new_paths=(
            "src/alfred_coo/fleet_hub/__init__.py",
            "src/alfred_coo/fleet_hub/metrics.py",
            "tests/test_fleet_hub_metrics.py",
            "plans/v1-ga/F23.md",
        ),
        base_branch="main",
        branch_hint="feature/f23-fleet-hub-metrics",
        notes="plan C §5 F23 + SAL-2631; hub /metrics adds fleet_endpoint_count{mode_state}, fleet_heartbeat_lag_seconds_p95, fleet_replication_lag_seconds; fleet_hub/ subtree is greenfield; APE/V: prometheus scrape returns >=3 series",
    ),

    "F24": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("src/alfred_coo/fleet_endpoint/__init__.py",),
        new_paths=(
            "src/alfred_coo/fleet_endpoint/telemetry.py",
            "tests/test_fleet_endpoint_telemetry.py",
            "plans/v1-ga/F24.md",
        ),
        base_branch="main",
        branch_hint="feature/f24-endpoint-telemetry",
        notes="plan C §5 F24 + SAL-2632; endpoint self-reports task counts, queue depth, fallback hits; sent piggy-backed on heartbeat; APE/V: 10 synthetic tasks → telemetry row in hub fleet_telemetry table",
    ),

    "SS-05": TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(),
        new_paths=(
            "tests/test_mcp_compat_smoke.py",
            "plans/v1-ga/SS-05.md",
        ),
        base_branch="main",
        branch_hint="feature/ss05-mcp-compat-smoke",
        notes="plan E §5.1 S-05 + SAL-2666; smoke every mcp__alfred__soul_* tool against a running soul-svc; matrix-test fixture with expected response shapes; APE/V: pytest tests/test_mcp_compat_smoke.py green; covers ≥10 MCP soul-* tools",
    ),

    "AB-22": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("src/alfred_coo/__init__.py",),
        new_paths=("plans/v1-ga/AB-22.md",),
        base_branch="main",
        branch_hint="feature/ab-22-umbrella",
        notes="Umbrella AB-22 hint — parser maps AB-22-{a..g} all to AB-22. Children dispatch with this hint; child must Step-2 verify and write its own paths into a sub-ticket-specific PR. Plan: deploy/appliance + tracing module split.",
    ),

    "AB-25": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("src/alfred_coo/autonomous_build/orchestrator.py",),
        new_paths=(
            "tests/test_decomposition_writes_plan_doc.py",
            "plans/v1-ga/AB-25.md",
        ),
        base_branch="main",
        branch_hint="feature/ab-25-decomp-writes-plan-doc",
        notes="AB-25 + SAL-3243; orchestrator auto-writes plans/v1-ga/<child>.md on decomposition; APE/V: synthetic decomposition produces a plan-doc commit alongside the child Linear issue",
    ),

    # ── Epic alfred-doctor (parent SAL-3280) ────────────────────────────
    # Hint-batch-2 (PR #270): 8 wave-3 children. Codes ``AD-a`` … ``AD-h``
    # required widening _CODE_RE in graph.py to recognise the letters-only
    # plan-doc prefix (no digits in the code). All 8 entries point into a
    # new ``src/alfred_coo/doctor/`` subpackage that the children will
    # create wholesale; one shared ``__init__.py`` is listed under
    # ``paths=`` so the builder reads it as the package anchor.

    "AD-A": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("src/alfred_coo/__init__.py",),
        new_paths=(
            "src/alfred_coo/doctor/__init__.py",
            "src/alfred_coo/doctor/ingest.py",
            "tests/doctor/__init__.py",
            "tests/doctor/test_ingest.py",
            "plans/v1-ga/AD-a.md",
        ),
        base_branch="main",
        branch_hint="feature/ad-a-ingest-sqlite-schema",
        notes="alfred-doctor seed ticket; new doctor/ subpackage + tests/doctor/ pytest tree; SQLite timeseries schema (events table, indices on (ts, severity)); APE/V: ingest 100 synthetic events → SELECT count(*) round-trips",
    ),

    "AD-B": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "src/alfred_coo/doctor/__init__.py",
            "src/alfred_coo/doctor/ingest.py",
        ),
        new_paths=(
            "src/alfred_coo/doctor/surveillance.py",
            "src/alfred_coo/doctor/heuristics.py",
            "tests/doctor/test_surveillance.py",
            "plans/v1-ga/AD-b.md",
        ),
        base_branch="main",
        branch_hint="feature/ad-b-surveillance-loop",
        notes="surveillance loop driven by claude-haiku; 12-pattern heuristic library (heuristics.py) + scanner over the AD-a SQLite store; APE/V: 12 synthetic events covering each pattern → surveillance flags ≥10/12 within one tick",
    ),

    "AD-C": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "src/alfred_coo/doctor/__init__.py",
            "src/alfred_coo/doctor/surveillance.py",
        ),
        new_paths=(
            "src/alfred_coo/doctor/slack_alerts.py",
            "tests/doctor/test_slack_alerts.py",
            "plans/v1-ga/AD-c.md",
        ),
        base_branch="main",
        branch_hint="feature/ad-c-slack-alerts",
        notes="Slack integration for Tier-2 alerts; reuse SLACK_BOT_TOKEN_ALFRED + #batcave conventions; APE/V: synthetic Tier-2 finding from AD-b → mocked Slack POST asserted with rendered body",
    ),

    "AD-D": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "src/alfred_coo/doctor/__init__.py",
            "src/alfred_coo/doctor/surveillance.py",
        ),
        new_paths=(
            "src/alfred_coo/doctor/investigator.py",
            "tests/doctor/test_investigator.py",
            "plans/v1-ga/AD-d.md",
        ),
        base_branch="main",
        branch_hint="feature/ad-d-investigation-loop",
        notes="investigation loop powered by claude-opus; consumes surveillance findings → produces structured root-cause hypotheses; APE/V: canned surveillance hit → investigator returns ≥1 hypothesis with cause + evidence",
    ),

    "AD-E": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "src/alfred_coo/doctor/__init__.py",
            "src/alfred_coo/doctor/investigator.py",
            "src/alfred_coo/doctor/slack_alerts.py",
        ),
        new_paths=(
            "src/alfred_coo/doctor/actions.py",
            "tests/doctor/test_actions.py",
            "plans/v1-ga/AD-e.md",
        ),
        base_branch="main",
        branch_hint="feature/ad-e-action-layer",
        notes="action layer; Tier-1 = automatic remediation, Tier-2 = Slack hand-off; gated by hypothesis confidence + allowlist; APE/V: Tier-1 fixture executes auto-action, Tier-2 fixture posts to Slack only",
    ),

    "AD-F": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "src/alfred_coo/doctor/__init__.py",
            "src/alfred_coo/doctor/investigator.py",
        ),
        new_paths=(
            "src/alfred_coo/doctor/hypothesis_tree.py",
            "tests/doctor/test_hypothesis_tree.py",
            "plans/v1-ga/AD-f.md",
        ),
        base_branch="main",
        branch_hint="feature/ad-f-hypothesis-tree",
        notes="hypothesis tree + recursion; depth-4 cap with explicit MAX_DEPTH constant; APE/V: synthetic seed expands to depth 4 then halts; depth-5 expansion attempt raises/short-circuits",
    ),

    "AD-G": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "src/alfred_coo/doctor/__init__.py",
            "src/alfred_coo/doctor/actions.py",
        ),
        new_paths=(
            "src/alfred_coo/doctor/learning.py",
            "tests/doctor/test_learning.py",
            "plans/v1-ga/AD-g.md",
        ),
        base_branch="main",
        branch_hint="feature/ad-g-learning-subsystem",
        notes="learning subsystem; runbook table (sqlite, separate file from AD-a events) + weekly summary generator; APE/V: 7 days of synthetic actions → weekly_summary() returns top-N runbook entries with hit counts",
    ),

    "AD-H": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "src/alfred_coo/doctor/__init__.py",
            "src/alfred_coo/doctor/surveillance.py",
            "src/alfred_coo/doctor/learning.py",
        ),
        new_paths=(
            "src/alfred_coo/doctor/dashboard_route.py",
            "tests/doctor/test_dashboard_route.py",
            "plans/v1-ga/AD-h.md",
        ),
        base_branch="main",
        branch_hint="feature/ad-h-v8-doctor-dashboard-route",
        notes="dashboard integration for v8-doctor; route defined in src/alfred_coo/doctor/dashboard_route.py for unit-test isolation, mounted later in /opt/v7-dashboard/dashboard.py via separate ops PR (out of scope here per Cristian's brief); APE/V: route returns 200 + JSON shape with surveillance + learning summary fields",
    ),

    # ── Epic OPS-14d (parent SAL-3038) ─────────────────────────────────
    # Hint-batch-2 (PR #270): 4 wave-3 children — `OPS-14d-{a,b,c,d}` —
    # all collapse to the single parsed code ``OPS-14D`` because
    # _CODE_RE captures only one trailing letter. Following the AB-22
    # umbrella precedent: one shared hint entry whose paths union covers
    # the whole subtree. Children read this hint, then write their own
    # PRs against the specific subset of files they own.

    "OPS-14D": TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "src/alfred_coo/auth/__init__.py",
            "src/alfred_coo/auth/scope_middleware.py",
        ),
        new_paths=(
            "src/alfred_coo/auth/ttl_constants.py",
            "src/alfred_coo/auth/ttl_validator.py",
            "tests/test_jwt_iat_shape.py",
            "tests/test_ttl_validator.py",
            "tests/e2e/__init__.py",
            "tests/e2e/test_ttl_end_to_end.py",
            "plans/v1-ga/OPS-14d-a.md",
            "plans/v1-ga/OPS-14d-b.md",
            "plans/v1-ga/OPS-14d-c.md",
            "plans/v1-ga/OPS-14d-d.md",
        ),
        base_branch="main",
        branch_hint="feature/ops-14d-scoped-token-ttl",
        notes="umbrella hint — _CODE_RE collapses OPS-14d-{a,b,c,d} all to OPS-14D; child must Step-2 verify against its own subset. AD-a = TTL constant + iat shape (ttl_constants.py + test_jwt_iat_shape.py). AD-b = validator + boundary tests (ttl_validator.py + test_ttl_validator.py). AD-c = wire validator into scope_middleware.py (modify) + targeted unit test. AD-d = end-to-end (tests/e2e/test_ttl_end_to_end.py: token at iat=now-25h → 401 via real middleware).",
    ),

}


def _render_target_block(
    code: str,
    vr: Optional[VerificationResult] = None,
) -> str:
    """Render a ``## Target`` markdown block for the given plan-doc code.

    Two rendering modes:

    * ``vr is None`` — legacy AB-13 behaviour: render hint verbatim from
      the static ``_TARGET_HINTS`` table, no verification comments. If
      ``code`` is not in ``_TARGET_HINTS``, emit the pre-existing
      ``(unresolved)`` escalation block so the child falls through to
      Step 0 of its persona grounding protocol. Preserved byte-for-byte
      so downstream snapshot tests keep passing.
    * ``vr is not None`` — AB-17-c verified-render mode: drive the block
      off ``vr.path_results``, rendering per the Plan I §3 decision
      table (``expected`` × ``observed`` ∈ {exist, absent, unknown}).
      Prepend a ``# VERIFICATION WARNING`` banner when
      ``vr.status == HintStatus.UNVERIFIED``. Defensive single-line
      fallback when ``vr.status == HintStatus.REPO_MISSING`` (should
      not happen — AB-17-d skips dispatch for REPO_MISSING — but keep
      it so a misuse surfaces loudly instead of rendering garbage).
    """
    # ── vr is None — legacy AB-13 path (must remain byte-identical) ─
    if vr is None:
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

    # ── vr is not None — AB-17-c verified-render path ───────────────

    # NO_HINT: mirror the legacy "no hint" escalation block so Plan I §3
    # "No hint (pre-existing behaviour, unchanged)" variant shows the
    # code that triggered it (tiny diagnostic upgrade — the legacy
    # string said "consult plan doc" with no code; the Plan I §3
    # example explicitly includes "no hint for code X").
    if vr.status == HintStatus.NO_HINT:
        return (
            "## Target\n"
            f"(unresolved — no hint for code {code}; consult plan doc; STOP "
            "and escalate via linear_create_issue per Step 0 of your "
            "persona protocol)\n"
        )

    # REPO_MISSING defensive fallback: AB-17-d is supposed to skip
    # dispatch entirely, so this function should never be called with a
    # REPO_MISSING vr. Render a one-liner instead of raising so a misuse
    # is visible in the child task body rather than crashing the
    # orchestrator mid-dispatch.
    if vr.status == HintStatus.REPO_MISSING:
        owner_repo = (
            f"{vr.hint.owner}/{vr.hint.repo}"
            if vr.hint is not None
            else f"(code {code})"
        )
        return (
            "## Target\n"
            f"(blocked — repo {owner_repo} missing; dispatch should not "
            "have happened; report bug)\n"
        )

    # From here on we need a hint — every non-NO_HINT / non-REPO_MISSING
    # status carries one (VerificationResult.hint is Optional only
    # because NO_HINT has no hint to attach).
    hint = vr.hint
    if hint is None:
        # Belt-and-braces: should be unreachable given the status enum
        # invariants, but don't let a None deref crash dispatch.
        return (
            "## Target\n"
            f"(blocked — verification result for code {code} has no hint "
            "but status is not NO_HINT; report bug)\n"
        )

    # Comment-alignment padding target. 48 chars matches the Plan I §3
    # rendered examples (``deploy/appliance/docker-compose.yml`` + a
    # handful of spaces + ``# verified exists @ main``).
    _PAD_WIDTH = 48

    def _pad(path: str) -> str:
        return " " * max(0, _PAD_WIDTH - len(path))

    # Split PathResults by expected axis so we can drive the two
    # sections (paths: / new_paths:) independently, and omit either
    # entirely if empty.
    exist_results: List[PathResult] = [
        pr for pr in vr.path_results if pr.expected == "exist"
    ]
    absent_results: List[PathResult] = [
        pr for pr in vr.path_results if pr.expected == "absent"
    ]

    def _render_exist(pr: PathResult) -> str:
        # expected=exist × observed={exist,absent,unknown}
        if pr.observed == "exist":
            return f"  - {pr.path}{_pad(pr.path)}# verified exists @ {hint.base_branch}"
        if pr.observed == "absent":
            return (
                f"  - (unresolved — file {pr.path} missing in "
                f"{hint.owner}/{hint.repo}@{hint.base_branch}; "
                f"check extension / casing / path; STOP and escalate per Step 0)"
            )
        # observed == "unknown"
        return (
            f"  - {pr.path}{_pad(pr.path)}# (unverified — {vr.error})"
        )

    def _render_absent(pr: PathResult) -> str:
        # expected=absent × observed={absent,exist,unknown}
        if pr.observed == "absent":
            return (
                f"  - {pr.path}{_pad(pr.path)}"
                f"# verified absent @ {hint.base_branch} — you will CREATE this file"
            )
        if pr.observed == "exist":
            # SAL-3281: NEW_PATHS_COLLISION → scaffold already exists but
            # other new_paths are still absent. Render a benign "scaffold
            # already exists" line so the persona does NOT escalate; the
            # ticket has real work left on the absent siblings. Reserve the
            # "STOP and escalate" wording for the legacy PATH_CONFLICT case
            # where the entire new_paths set already shipped (i.e. the
            # ticket's work was merged in an earlier wave/run and the
            # persona should grounding-gap escalate).
            if vr.status == HintStatus.NEW_PATHS_COLLISION:
                return (
                    f"  - {pr.path}{_pad(pr.path)}"
                    f"# scaffold already exists @ {hint.base_branch} — "
                    f"leave existing content; build the absent siblings"
                )
            return (
                f"  - (conflict — file {pr.path} already exists in "
                f"{hint.owner}/{hint.repo}@{hint.base_branch}; "
                f"was it created by an earlier wave? STOP and escalate per Step 0)"
            )
        # observed == "unknown"
        return (
            f"  - {pr.path}{_pad(pr.path)}# (unverified — {vr.error}) — expected NEW"
        )

    lines: List[str] = []
    if vr.status == HintStatus.UNVERIFIED:
        lines.append(
            "# VERIFICATION WARNING: hint could not be verified against "
            "live repo state. Child MUST re-verify in Step 2."
        )
    lines.append("## Target")
    lines.append(f"owner: {hint.owner}")
    lines.append(f"repo:  {hint.repo}")

    if exist_results:
        lines.append("paths:")
        for pr in exist_results:
            lines.append(_render_exist(pr))

    if absent_results:
        lines.append("new_paths:")
        for pr in absent_results:
            lines.append(_render_absent(pr))

    lines.append(f"base_branch: {hint.base_branch}")
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
        # AB-17-w: overridable per-kickoff via `wave_green_ratio_threshold`.
        self.wave_green_ratio_threshold: float = DEFAULT_WAVE_GREEN_RATIO_THRESHOLD

        # SAL-2870 retry + deadlock-grace tunables. All three are overrideable
        # via top-level kickoff payload fields (``retry_budget``,
        # ``retry_backoff_sec``, ``deadlock_grace_sec``). ``retry_budget`` is
        # the *default* applied to every Ticket at graph-build time — per-
        # ticket overrides via Ticket.retry_budget take precedence (e.g.
        # restored state). Setting ``retry_budget=0`` disables the BACKED_OFF
        # path and restores legacy "FAILED is terminal on first failure"
        # semantics.
        self.retry_budget: int = DEFAULT_RETRY_BUDGET
        self.retry_backoff_sec: int = DEFAULT_RETRY_BACKOFF_SEC
        self.deadlock_grace_sec: int = DEFAULT_DEADLOCK_GRACE_SEC
        # Wall-clock when (in_flight=0 AND ready=0) was first observed. Reset
        # to None whenever in_flight or ready becomes non-empty. Coerce-to-
        # FAILED only fires when ``now - _no_progress_since >=
        # deadlock_grace_sec``. Persisted into state for restart safety.
        self._no_progress_since: Optional[float] = None

        # Stash the last time we posted a cadence tick so _status_tick can
        # rate-limit itself without a separate timer.
        self._last_cadence_ts: float = 0.0

        # AB-17-p: wall-clock of the last observed forward-progress event
        # (successful dispatch, state transition in _poll_children, or
        # review verdict handled in _poll_reviews). The _dispatch_wave
        # watchdog compares `time.time() - self._last_progress_ts` against
        # PROGRESS_STALL_WARN_SEC to emit a "[watchdog] wave N no forward
        # progress" warning. Seeded to now() so a fresh orchestrator
        # doesn't immediately trip the threshold before its first tick.
        self._last_progress_ts: float = time.time()

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

        # AB-17-b · Plan I §1 — pre-dispatch hint verification. Populated
        # at the top of each wave by `_verify_wave_hints`; read by AB-17-c's
        # `_render_target_block` to decorate the child target block with
        # live-GitHub state. The semaphore caps concurrent GitHub API
        # fan-out so we don't trip abuse detection on a 16-ticket wave.
        self._verified_hints: Dict[str, "VerificationResult"] = {}
        self._verify_semaphore: asyncio.Semaphore = asyncio.Semaphore(8)

        # AB-17-d · Plan I §1.4 + §2.3 — orchestrator-side BLOCKED handling
        # for REPO_MISSING hints. `_repo_missing_tickets` holds Ticket.id
        # (UUID) values for tickets the wave gate must exclude from pass/fail
        # bookkeeping (they were never dispatched). `_emitted_blocks` dedupes
        # grounding-gap Linear emissions within a single orchestrator process
        # so a ticket that lingers across multiple wave re-entries doesn't
        # spam duplicate issues. Both reset on restart — re-emission on the
        # next process is acceptable per Plan I §5.1 R-d (idempotent enough
        # for MVP).
        self._repo_missing_tickets: set[str] = set()
        self._emitted_blocks: set[str] = set()

        # SAL-3070 / SAL-3038 phantom-loop circuit breakers (2026-04-28).
        # ``_consecutive_phantoms`` maps ticket UUID → list of monotonic
        # timestamps when ``_reset_phantom_failure`` fired for that ticket.
        # Pruned to the trailing ``PHANTOM_LOOP_WINDOW_SEC`` window on every
        # phantom-cleanup; if the post-prune size hits
        # ``PHANTOM_LOOP_MAX_IN_WINDOW`` the ticket is force-escalated.
        # ``_wave_progress_history`` maps wave_n → list of
        # (monotonic_ts, green_count) tuples, capped at the last 20 entries.
        # Sampled from the dispatch loop on every tick. If the latest entry's
        # green_count hasn't increased over the trailing
        # ``WAVE_STALL_FORCE_PASS_SEC`` window the wave is force-passed and
        # remaining non-terminal tickets are escalated for human review.
        # Both reset on restart — the live-incident signal is
        # within-process, the saved soul state already captures terminal
        # transitions per-ticket.
        self._consecutive_phantoms: Dict[str, List[float]] = {}
        self._wave_progress_history: Dict[int, List[Tuple[float, int]]] = {}

        # Sequential-discipline Fix 2 (2026-04-28): per-ticket dispatch
        # idempotency. Maps ticket UUID → child_task_id of the most
        # recently-dispatched in-flight child. Used by ``_dispatch_child``
        # to refuse a duplicate dispatch when an earlier one is still
        # outstanding — prevents the "10+ pending tasks for SAL-3070"
        # orphan storm observed 2026-04-28 when phantom-cleanup fired
        # repeatedly on a still-running child. Cleared by
        # ``_release_in_flight_dispatch`` on terminal transition (PR_OPEN,
        # FAILED, ESCALATED, MERGED_GREEN) and by the hard-timeout
        # branch in ``_poll_children``. Process-local — restored state
        # rebuilds it lazily as live children re-enter the in-flight
        # accounting.
        self._in_flight_dispatches: Dict[str, str] = {}

        # Sequential-discipline Fix 3 (2026-04-28): builder model fallback
        # chain. Resolved from the kickoff payload's
        # ``builder_fallback_chain`` field; falls back to the module-level
        # ``DEFAULT_BUILDER_FALLBACK_CHAIN``. ``_dispatch_child`` reads
        # ``ticket.dispatch_attempts % len(chain)`` to pick the model for
        # the current dispatch and stamps a ``[tag:code]`` /
        # ``[tag:strategy]`` marker on the child task title so
        # ``dispatch.select_model`` routes correctly downstream.
        self.builder_fallback_chain: Tuple[str, ...] = (
            DEFAULT_BUILDER_FALLBACK_CHAIN
        )

        # AB-17-q · external cancel signal (SAL-2756, 2026-04-24).
        # An operator who PATCHes the kickoff task's lifecycle state to
        # ``failed`` (with ``result.cancel == True``, or just ``status ==
        # "failed"``) signals this orchestrator to drain gracefully. Set
        # by ``_check_cancel_signal`` once per dispatch tick. When True:
        #   - ``_drain_mode`` is also flipped on so ``_dispatch_wave``
        #     stops selecting new ready tickets.
        #   - the wave loop in ``_run_inner`` exits cleanly after the
        #     current wave's in-flight children finish (or are skipped
        #     by the gate-deadlock detector if they crashed).
        #   - ``_complete_kickoff_canceled`` runs instead of
        #     ``_run_on_all_green_actions`` + ``_complete_kickoff``, so
        #     the kickoff record stays consistent with the operator's
        #     intent rather than racing them to a ``completed`` write.
        # Replaces the restart-as-cancel pattern (full daemon bounce
        # observed costing ~60s and killing in-flight builds, 2026-04-24
        # task db4a7b9f).
        self._cancel_requested: bool = False
        self._cancel_reason: str = ""

        # SAL-3038 PR-exists short-circuit (2026-04-28). Maps ticket
        # identifier (e.g. "SAL-3038") -> (cached_at_ts, pr_number_or_None).
        # Read by ``_ticket_has_open_pr_awaiting_review`` to skip GitHub
        # round-trips on dispatch decisions that fire repeatedly within
        # the cache window. SAL-3038 itself was re-dispatched 68 times in
        # 7 days because the dispatch loop never checked whether an open
        # PR awaiting Hawkman review already existed for the ticket; each
        # cycle burned $0.50-2 producing nothing useful. Cache window is
        # short enough (5 min) that a real Hawkman approval lands on the
        # following dispatch tick at the latest.
        self._pr_exists_cache: Dict[str, Tuple[float, Optional[int]]] = {}
        # Process-local counter so an operator tailing logs / state events
        # can distinguish "wave is genuinely idle" from "wave is short-
        # circuiting on PRs awaiting review". Reset on restart.
        self._pr_exists_skips: int = 0
        # Test hook: setting ``_gh_pr_open_search_fn`` on the instance
        # bypasses the live GitHub round-trip in
        # ``_ticket_has_open_pr_awaiting_review``. Mirrors the
        # ``_gh_pr_search_fn`` / ``_gh_pr_files_fn`` stubs already used by
        # the stale-sweep helpers above. Stub signature:
        #   async (ticket_ident: str) -> Optional[Dict[str, Any]]
        # Returning a dict with at least {"number": int, "created_at":
        # ISO-8601 str, "reviews": [{"user": {"login": str}, "state": str},
        # ...]} satisfies the helper's parsing path; returning None means
        # "no PR found".
        self._gh_pr_open_search_fn = None

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

        # Fix B (stale In Progress sweeper): on startup, sweep Linear for
        # tickets stuck in "In Progress" whose PR was already merged. Eats
        # up wave-1 deadlock-grace cycles otherwise (e.g. SAL-2656 OPS-23
        # observed in v7ab/v7ac/v7ad). Best-effort, bounded to the active
        # project, idempotent — never raises into the wave loop.
        try:
            await self._sweep_stale_in_progress(self.linear_project_id)
        except Exception:
            logger.exception(
                "_sweep_stale_in_progress crashed during startup; continuing"
            )

        # 4. Main wave loop.
        for wave in self.wave_order:
            self.state.current_wave = wave
            logger.info("entering wave %d", wave)
            self.state.record_event("wave_enter", wave=wave)
            # Fix A (wave-skip cache): if this wave passed at ratio=1.00
            # within the freshness window AND no Linear ticket has moved
            # backward since, short-circuit the wave entirely. Saves one
            # full hint-verify + dispatch + gate cycle per still-green
            # wave on each kickoff/restart. Best-effort — any soul lookup
            # hiccup falls back to normal evaluation.
            try:
                if await self._should_skip_wave(wave):
                    logger.info(
                        "[wave-skip] wave=%d pre-passed at 1.00 in last run; "
                        "skipping re-evaluation", wave,
                    )
                    self.state.record_event(
                        "wave_skipped_pre_passed",
                        wave=wave,
                        reason="prior_pass_ratio_1.00_within_freshness_window",
                    )
                    await checkpoint(self.state, self.soul, self.task_id)
                    continue
            except Exception:
                logger.exception(
                    "_should_skip_wave crashed for wave %d; falling through "
                    "to full evaluation", wave,
                )
            # AB-17-b · Plan I §1: verify every ticket's TargetHint against
            # live GitHub state BEFORE dispatch so the rendered ## Target
            # block in the child task body can carry an `observed:` row.
            # Verification is best-effort — UNVERIFIED / NO_HINT still
            # dispatch (BLOCKED handling is AB-17-d). Logged as a status
            # histogram so operators can spot a wave with lots of
            # REPO_MISSING / PATH_CONFLICT before children start.
            try:
                self._verified_hints = await self._verify_wave_hints(wave)
                status_counts = Counter(
                    vr.status for vr in self._verified_hints.values()
                )
                logger.info(
                    "wave %d hint verification: %s",
                    wave,
                    {k.value: v for k, v in status_counts.items()},
                )
            except Exception:
                logger.exception(
                    "wave %d hint verification crashed; dispatching without "
                    "verified hints",
                    wave,
                )
                self._verified_hints = {}
            await self._dispatch_wave(wave)
            # AB-17-q · external cancel signal (SAL-2756). If
            # `_dispatch_wave` returned because the operator canceled the
            # run (drain finished), skip the wave gate (it would raise on
            # non-terminal tickets that never got dispatched) and exit
            # the wave loop. Graceful-cancel terminal handler runs below.
            if self._cancel_requested:
                logger.info(
                    "wave %d: cancel observed during dispatch; skipping "
                    "wave gate and exiting wave loop", wave,
                )
                self.state.record_event("wave_exit_canceled", wave=wave)
                break
            await self._wait_for_wave_gate(wave)
            self.state.record_event("wave_exit", wave=wave)
            await checkpoint(self.state, self.soul, self.task_id)

        # 5. AB-17-q: branch on cancel before running on_all_green or the
        # standard complete-kickoff path. on_all_green spawns more child
        # tasks (the post-merge actions) which would defeat the cancel
        # intent; route to the cancel-terminal helper instead.
        if self._cancel_requested:
            await self._complete_kickoff_canceled()
            return

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

        # AB-17-w: per-kickoff override of the wave-gate green-ratio
        # threshold. Default is SOFT_GREEN_THRESHOLD (0.9). The payload
        # field is `wave_green_ratio_threshold` (top-level float).
        gate_override = payload.get("wave_green_ratio_threshold")
        if gate_override is not None:
            try:
                self.wave_green_ratio_threshold = float(gate_override)
            except (TypeError, ValueError):
                logger.warning(
                    "ignoring non-numeric wave_green_ratio_threshold=%r; "
                    "keeping default %.2f",
                    gate_override, DEFAULT_WAVE_GREEN_RATIO_THRESHOLD,
                )
                self.wave_green_ratio_threshold = (
                    DEFAULT_WAVE_GREEN_RATIO_THRESHOLD
                )

        # SAL-2870: retry budget + backoff window + deadlock grace.
        # All three are top-level optional ints/floats on the kickoff
        # payload. Bad values fall back to module defaults with a WARN —
        # we never crash the run on a typo.
        retry_budget_override = payload.get("retry_budget")
        if retry_budget_override is not None:
            try:
                self.retry_budget = max(0, int(retry_budget_override))
            except (TypeError, ValueError):
                logger.warning(
                    "ignoring non-integer retry_budget=%r; keeping default %d",
                    retry_budget_override, DEFAULT_RETRY_BUDGET,
                )
                self.retry_budget = DEFAULT_RETRY_BUDGET

        backoff_override = payload.get("retry_backoff_sec")
        if backoff_override is not None:
            try:
                self.retry_backoff_sec = max(0, int(backoff_override))
            except (TypeError, ValueError):
                logger.warning(
                    "ignoring non-integer retry_backoff_sec=%r; keeping "
                    "default %ds",
                    backoff_override, DEFAULT_RETRY_BACKOFF_SEC,
                )
                self.retry_backoff_sec = DEFAULT_RETRY_BACKOFF_SEC

        grace_override = payload.get("deadlock_grace_sec")
        if grace_override is not None:
            try:
                self.deadlock_grace_sec = max(0, int(grace_override))
            except (TypeError, ValueError):
                logger.warning(
                    "ignoring non-integer deadlock_grace_sec=%r; keeping "
                    "default %ds",
                    grace_override, DEFAULT_DEADLOCK_GRACE_SEC,
                )
                self.deadlock_grace_sec = DEFAULT_DEADLOCK_GRACE_SEC

        # Wave order.
        wave_order = payload.get("wave_order")
        if isinstance(wave_order, list) and wave_order:
            self.wave_order = [int(w) for w in wave_order if isinstance(w, (int, str))]

        # Sequential-discipline Fix 3 (2026-04-28): builder model fallback
        # chain override. Accepts a list of model strings; non-list / empty
        # values fall back to the module default with a WARN so a typo
        # never crashes the run.
        chain_override = payload.get("builder_fallback_chain")
        if chain_override is not None:
            if (
                isinstance(chain_override, (list, tuple))
                and chain_override
                and all(isinstance(m, str) and m for m in chain_override)
            ):
                self.builder_fallback_chain = tuple(chain_override)
            else:
                logger.warning(
                    "ignoring invalid builder_fallback_chain=%r; "
                    "keeping default %r",
                    chain_override, DEFAULT_BUILDER_FALLBACK_CHAIN,
                )
                self.builder_fallback_chain = DEFAULT_BUILDER_FALLBACK_CHAIN

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
        # SAL-2870: seed every ticket with the kickoff-configured retry
        # budget BEFORE per-ticket restore so a ticket without a stored
        # ``retry_count`` simply inherits the new default. Per-ticket
        # restores below override on top.
        for node in self.graph.nodes.values():
            node.retry_budget = self.retry_budget
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
            # SAL-2870: restore retry_count + backed_off_at so a daemon
            # bounce inside the cooling window resumes the same backoff
            # timer rather than starting over.
            rc = (self.state.retry_counts or {}).get(uuid)
            if isinstance(rc, int) and rc > 0:
                node.retry_count = rc
            ba = (self.state.backed_off_at or {}).get(uuid)
            if isinstance(ba, (int, float)) and ba > 0:
                node.backed_off_at = float(ba)
        # SAL-2870: restore the deadlock-grace timer. ``None`` is a valid
        # value (no active no-progress streak) so we copy the field directly
        # without a falsy guard.
        if hasattr(self.state, "no_progress_since"):
            self._no_progress_since = self.state.no_progress_since

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
            # SAL-2870: mirror retry_count + backed_off_at so a restart
            # mid-backoff resumes the same cooling timer. ``backed_off_at``
            # is cleared (set to None) when a ticket leaves BACKED_OFF, so
            # we mirror absence by popping the dict key — keeps the state
            # snapshot diff-friendly.
            if ticket.retry_count:
                self.state.retry_counts[uuid] = ticket.retry_count
            elif uuid in (self.state.retry_counts or {}):
                self.state.retry_counts.pop(uuid, None)
            if ticket.backed_off_at:
                self.state.backed_off_at[uuid] = ticket.backed_off_at
            elif uuid in (self.state.backed_off_at or {}):
                self.state.backed_off_at.pop(uuid, None)
        # SAL-2870: mirror the deadlock-grace timer onto state so a
        # daemon bounce mid-grace-window resumes the same start point.
        self.state.no_progress_since = self._no_progress_since

    # ── dispatch ────────────────────────────────────────────────────────────

    async def _dispatch_wave(self, wave_n: int) -> None:
        """Dispatch + poll tickets in `wave_n` until every one of them is in
        a terminal state. Inner loop = one 45s tick."""
        wave_tickets = self.graph.tickets_in_wave(wave_n)
        if not wave_tickets:
            logger.info("wave %d has no tickets; skipping", wave_n)
            return

        # AB-17-d · Plan I §1.4 + §2.3 — skip dispatch for tickets whose
        # hint verification returned REPO_MISSING. The repo does not exist
        # on GitHub (verified at wave start by `_verify_wave_hints`), so
        # any child we spawned would open a PR against a non-existent
        # base, fail `gh pr create`, and loop into zombie-guard retries.
        # Instead: emit a grounding-gap Linear issue (idempotent per
        # process via `_emitted_blocks`) and mark the ticket FAILED
        # internally so the wave loop can terminate. Linear state is NOT
        # mutated — the parent ticket stays Backlog (per Plan I §5.1 R-d,
        # MVP keeps BLOCKED implicit; no new Linear state or label). The
        # wave gate (`_wait_for_wave_gate`) excludes these from the
        # soft-green numerator/denominator via `_repo_missing_tickets`.
        await self._mark_repo_missing_tickets(wave_tickets)

        while True:
            # AB-17-q · external cancel signal (SAL-2756). Polled at the
            # top of every tick so the operator's PATCH is observed within
            # one `poll_sleep_sec` cycle (45s by default). Sets
            # `_drain_mode` so the dispatch loop below skips new children
            # automatically; the early-exit check at the bottom of the
            # tick lets us break the moment in-flight drains.
            await self._check_cancel_signal()

            # ── select ready ────────────────────────────────────────────
            in_flight = self._in_flight_for_wave(wave_n)
            ready = self._select_ready(wave_tickets, in_flight)

            # AB-17-p: per-tick liveness trace so an operator tailing
            # DEBUG logs can distinguish "orchestrator ticking but not
            # logging" from "orchestrator genuinely hung" without waiting
            # for the 20-min _status_tick cadence.
            logger.debug(
                "[tick] wave=%d in_flight=%d ready=%d",
                wave_n, len(in_flight), len(ready),
            )

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
                # Human-assigned label check (AB-17-v dispatch-side enforcement).
                # Wave-gate already excuses human-assigned tickets, but the
                # dispatch path historically still ran builders against them,
                # producing stub PRs that flipped tickets Done in error
                # (2026-04-27 incident — SAL-2641, SAL-2647, +4 phantom flips).
                #
                # SAL-3038 / SAL-3070 (2026-04-28): the predicate now lives
                # in `alfred_coo.main._should_skip_for_human_or_terminal` so
                # the bare-claim path in `main.py` (which never hydrates a
                # `Ticket`) can apply the same gate. We adapt the hydrated
                # `Ticket` to the predicate's dict shape; we deliberately
                # do NOT pass a state — terminal-state tickets are already
                # filtered out earlier in hydration, and mapping
                # `TicketStatus` → Linear state names here would couple
                # the orchestrator to Linear's vocabulary.
                #
                # Lazy import: `alfred_coo.main` transitively imports the
                # orchestrator module via `_resolve_handler`, so a
                # top-level import here would create a cycle.
                from ..main import _should_skip_for_human_or_terminal
                _gate_skip, _gate_reason = _should_skip_for_human_or_terminal({
                    "labels": getattr(ticket, "labels", None) or [],
                    "state": "",
                })
                if _gate_skip:
                    logger.info(
                        "%s has human-assigned label; skipping dispatch "
                        "(treated as terminal-success; reason=%s)",
                        ticket.identifier, _gate_reason,
                    )
                    ticket.status = TicketStatus.ESCALATED
                    continue
                # SAL-3038 (2026-04-28): if an open PR for this ticket is
                # already awaiting Hawkman review, skip dispatch and let
                # the review path drive the ticket forward. Closes the
                # 68-redispatch-storm observed on SAL-3038 / PR #214.
                # Returns None when no such PR exists, the PR is too old
                # (>7 days), Hawkman has approved, or GitHub is
                # unreachable — in all those cases dispatch proceeds.
                existing_pr = await self._ticket_has_open_pr_awaiting_review(
                    ticket.identifier,
                )
                if existing_pr is not None:
                    self._pr_exists_skips += 1
                    logger.info(
                        "skipping dispatch: PR #%d exists for ticket %s, "
                        "awaiting Hawkman review (skips=%d)",
                        existing_pr, ticket.identifier,
                        self._pr_exists_skips,
                    )
                    self.state.record_event(
                        "pr_exists_skip",
                        identifier=ticket.identifier,
                        pr_number=existing_pr,
                        skips_total=self._pr_exists_skips,
                    )
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

            # ── SAL-3070 / SAL-3038 wave-stall force-pass (2026-04-28) ──
            # Sample (monotonic_ts, green_count) for this wave on every
            # tick. If the green_count hasn't increased over the trailing
            # ``WAVE_STALL_FORCE_PASS_SEC`` window, force-pass the wave:
            # escalate every remaining non-terminal ticket so the operator
            # sees a clear "human-assigned" handoff and the orchestrator
            # moves on rather than spinning indefinitely on un-mergeable
            # work. A wave whose green_count is still rising is never
            # force-passed — only stall-pass.
            if await self._maybe_force_pass_stalled_wave(
                wave_n, wave_tickets,
            ):
                break

            # ── exit condition ──────────────────────────────────────────
            if all(t.status in TERMINAL_STATES for t in wave_tickets):
                logger.info(
                    "wave %d all tickets terminal; breaking dispatch loop",
                    wave_n,
                )
                break

            # AB-17-q · graceful-cancel exit. Once cancel is requested AND
            # no children remain in-flight for this wave, the drain is
            # complete: break out of the dispatch loop without raising.
            # The wave gate is skipped by `_run_inner` so non-terminal
            # tickets that never got dispatched don't trip the gate's
            # critical-path / soft-green math. `in_flight` was computed
            # at the top of this tick — re-checking against the current
            # ticket statuses (which `_poll_children` may have just
            # advanced) is what we want.
            if self._cancel_requested:
                still_in_flight = self._in_flight_for_wave(wave_n)
                if not still_in_flight:
                    logger.warning(
                        "[cancel] wave %d drained (no in-flight); "
                        "exiting dispatch loop on cancel signal",
                        wave_n,
                    )
                    self.state.record_event(
                        "wave_dispatch_canceled",
                        wave=wave_n,
                        reason=self._cancel_reason,
                    )
                    break

            # AB-17-n + SAL-2870: detect and break deadlock where non-terminal
            # tickets (typically BLOCKED on FAILED upstreams) cannot progress
            # because _deps_satisfied permanently returns False.
            #
            # SAL-2870 (2026-04-25, post-v7o crash 18:09:19 UTC) replaces the
            # original same-tick coerce-to-FAILED with a grace window. v7o
            # cascaded 17 wave-1 tickets to FAILED the moment SS-10 et al.
            # FAILED, before the retry loop had a chance to re-dispatch
            # those upstreams. Now:
            #   1. The first observation of `in_flight=0 + ready=0` arms
            #      ``self._no_progress_since = now``.
            #   2. While armed, every subsequent tick that ALSO sees
            #      ``in_flight=0 + ready=0`` checks elapsed time; if
            #      ``deadlock_grace_sec`` has passed → coerce. Otherwise
            #      keep ticking so the BACKED_OFF→READY flip in
            #      ``_poll_children`` (SAL-2870 #2) can lift the wave.
            #   3. As soon as in_flight or ready becomes non-empty (e.g. a
            #      backed-off ticket woke up and got dispatched), the
            #      timer is reset to None.
            # BACKED_OFF tickets count as non-terminal but produce neither
            # in_flight nor ready until the cooling window elapses, so the
            # grace timer must keep them alive — that's by design.
            non_terminal = [
                t for t in wave_tickets if t.status not in TERMINAL_STATES
            ]
            if not in_flight and not ready:
                if self._no_progress_since is None:
                    self._no_progress_since = time.time()
                    logger.info(
                        "wave %d deadlock-grace armed: in_flight=0 ready=0; "
                        "waiting %ds before coerce",
                        wave_n, self.deadlock_grace_sec,
                    )
                stuck_for = time.time() - self._no_progress_since
                if non_terminal and stuck_for >= self.deadlock_grace_sec:
                    blocked_ids = [t.identifier for t in non_terminal]
                    logger.error(
                        "wave %d deadlock: %d tickets non-terminal with no "
                        "in-flight or ready for %.0fs (grace=%ds); coercing "
                        "to FAILED: %s",
                        wave_n, len(non_terminal), stuck_for,
                        self.deadlock_grace_sec, blocked_ids,
                    )
                    for t in non_terminal:
                        upstream_failed = [
                            self.graph.nodes[u].identifier
                            for u in (t.blocks_in or [])
                            if u in self.graph.nodes
                            and self.graph.nodes[u].status == TicketStatus.FAILED
                        ]
                        t.status = TicketStatus.FAILED
                        self.state.record_event(
                            "ticket_forced_failed_deadlock",
                            identifier=t.identifier,
                            upstream_failed=upstream_failed,
                            stuck_for_sec=int(stuck_for),
                        )
                    self._no_progress_since = None
                    break
            else:
                # Forward progress observed (something is in-flight or ready).
                # Reset the grace timer so a later flat tick re-arms cleanly.
                if self._no_progress_since is not None:
                    logger.debug(
                        "wave %d deadlock-grace cleared: in_flight=%d ready=%d",
                        wave_n, len(in_flight), len(ready),
                    )
                self._no_progress_since = None

            # AB-17-p: no-forward-progress visibility. Emits a WARN + state
            # event when the wave has had in-flight work but no progress
            # event (dispatch / poll_children transition / review verdict)
            # for >PROGRESS_STALL_WARN_SEC. Pairs with AB-17-n's structural
            # deadlock break above: AB-17-n catches "nothing in-flight AND
            # nothing ready" (stuck graph), this catches "in-flight but
            # frozen" (stuck sub / review task). Visibility ONLY — does not
            # cancel, retry, or mark failed.
            stall_sec = time.time() - self._last_progress_ts
            if stall_sec > PROGRESS_STALL_WARN_SEC and in_flight:
                logger.warning(
                    "[watchdog] wave %d no forward progress for %.0fs; "
                    "in_flight=%d ready=%d",
                    wave_n, stall_sec, len(in_flight), len(ready),
                )
                self.state.record_event(
                    "wave_no_progress",
                    wave=wave_n, stall_sec=stall_sec,
                    in_flight=len(in_flight), ready=len(ready),
                )

            await asyncio.sleep(self.poll_sleep_sec)

    def _select_ready(
        self,
        wave_tickets: List[Ticket],
        in_flight: List[Ticket],
    ) -> List[Ticket]:
        """Return tickets in `pending` whose `blocks_in` are all merged_green.

        Sort: critical-path first, then topological order (deps first,
        dependents later) within the same critical-path tier, then by
        identifier as the final tiebreaker. SAL-2870 #5 added the topo
        layer between the existing CP and identifier sorts so that when
        two tickets are both ready and both/neither critical-path, the
        one whose dependency chain is shallower is dispatched first.
        Critical-path always wins because the operator may have flagged
        a critical-path-leaf-but-deep-tree ticket as the priority pin
        (e.g. SS-08 for a post-gap rerun).
        """
        in_flight_ids = {t.id for t in in_flight}
        ready: List[Ticket] = []
        for t in wave_tickets:
            # Only PENDING or BLOCKED tickets can (re-)enter the dispatch
            # queue. Terminal + in-flight + cooling states are filtered out.
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
        # SAL-2870 #5: topo sort BEFORE the CP sort so the final ordering
        # is (CP-tier, topo-rank, identifier). Python's sort is stable,
        # so applying topo first then CP gives the right result.
        ready = self._topo_sort(ready)
        ready.sort(key=lambda x: (not x.is_critical_path, x.identifier))
        # Re-apply topo within each CP tier. We do this by re-grouping
        # because the identifier-tiebreak above can disrupt topo order.
        ready = self._stable_topo_within_cp(ready)
        return ready

    def _topo_sort(self, tickets: List[Ticket]) -> List[Ticket]:
        """SAL-2870 #5: Kahn-style topological sort over the subset of
        ``tickets`` using the orchestrator graph's ``blocks_in`` /
        ``blocks_out`` edges. Dependencies appear before dependents.

        Edges referencing tickets outside ``tickets`` are ignored — the
        sort runs over a candidate slice (typically the current ready
        set), not the full graph. Cycles, if any, fall through with the
        remaining items appended in identifier order so the function
        always returns ``len(tickets)`` items.

        O(V+E) on the candidate slice; cheap for any realistic wave.
        """
        if not tickets:
            return tickets
        candidate_ids = {t.id for t in tickets}
        # Edges restricted to the candidate set so cross-wave / merged
        # upstreams don't pollute the indegree count.
        indeg: Dict[str, int] = {t.id: 0 for t in tickets}
        adj: Dict[str, List[str]] = {t.id: [] for t in tickets}
        for t in tickets:
            for dep_id in (t.blocks_in or []):
                if dep_id in candidate_ids:
                    indeg[t.id] += 1
                    adj[dep_id].append(t.id)
        ticket_by_id = {t.id: t for t in tickets}
        # Stable initial frontier: all zero-indegree nodes ordered by
        # (not is_critical_path, identifier) so two roots without deps
        # come out in the same order the legacy sort would have produced.
        frontier = sorted(
            [tid for tid in indeg if indeg[tid] == 0],
            key=lambda tid: (
                not ticket_by_id[tid].is_critical_path,
                ticket_by_id[tid].identifier,
            ),
        )
        ordered: List[Ticket] = []
        seen: set[str] = set()
        while frontier:
            tid = frontier.pop(0)
            if tid in seen:
                continue
            seen.add(tid)
            ordered.append(ticket_by_id[tid])
            # Generate next frontier in deterministic order so retries
            # produce identical traces.
            nxt = []
            for child in adj.get(tid, []):
                indeg[child] -= 1
                if indeg[child] == 0 and child not in seen:
                    nxt.append(child)
            nxt.sort(key=lambda x: (
                not ticket_by_id[x].is_critical_path,
                ticket_by_id[x].identifier,
            ))
            frontier.extend(nxt)
        # Cycles / unreachable nodes — append in identifier order so the
        # caller still receives every ticket exactly once.
        if len(ordered) < len(tickets):
            remainder = sorted(
                [t for t in tickets if t.id not in seen],
                key=lambda t: t.identifier,
            )
            logger.warning(
                "SAL-2870 _topo_sort: %d ticket(s) outside topo order "
                "(possible cycle): %s",
                len(remainder),
                [t.identifier for t in remainder],
            )
            ordered.extend(remainder)
        return ordered

    def _stable_topo_within_cp(self, ordered: List[Ticket]) -> List[Ticket]:
        """SAL-2870 #5 (helper): preserve topo order *within* each
        critical-path tier after the (CP-first, identifier-tiebreak) sort.
        ``ready.sort(...)`` above can pull a topo-later ticket ahead of
        its dependency if the deeper ticket has a smaller identifier.
        Re-toposort each CP tier independently to fix that.
        """
        if not ordered:
            return ordered
        cp = [t for t in ordered if t.is_critical_path]
        non_cp = [t for t in ordered if not t.is_critical_path]
        return self._topo_sort(cp) + self._topo_sort(non_cp)

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
            if t.status in ACTIVE_TICKET_STATES
        ]

    def _epic_in_flight(self, epic: str, in_flight: List[Ticket]) -> int:
        return sum(1 for t in in_flight if t.epic == epic)

    async def _dispatch_child(self, ticket: Ticket) -> None:
        """Create a `[persona:alfred-coo-a]` child mesh task for `ticket`,
        mark Linear `In Progress`, and stamp the ticket as dispatched.

        Uses `self.mesh.create_task(...)` — added to MeshClient alongside
        this orchestrator (plan F §4.2 notes mesh_task_create as re-used).

        SAL-2787: re-verify the target hint immediately before dispatch to
        defeat the wave-cache staleness race. ``_verify_wave_hints`` runs
        ONCE at wave start and caches in ``self._verified_hints``; sibling
        builders may merge ``new_paths`` mid-wave, so by the time a later
        child dispatches the cached entry can be stale (v7e wave 0,
        2026-04-24: 6 dispatches → 0 PRs because every child correctly
        grounded out on a stale OK that re-verification flipped to
        PATH_CONFLICT). Reuses ``self._verify_semaphore`` via
        ``_verify_hint``; ~200ms HTTP cost per dispatch.

        Wave-start ``_verify_wave_hints`` is preserved (cadence display +
        initial graph signal); this just refreshes the per-ticket entry
        in-place so ``_child_task_body`` reads fresh state. Failures here
        fall back to the cached entry (verification crashes mid-wave must
        not freeze dispatch — UNVERIFIED still dispatches by design).

        Sequential-discipline (2026-04-28):
          - Fix 2: per-ticket dispatch idempotency. Refuse to dispatch a
            ticket that already has an in-flight child registered in
            ``self._in_flight_dispatches``. Prevents the orphan storm
            (10+ pending tasks for a single ticket) observed when
            phantom-cleanup races a still-running child.
          - Fix 3: builder model fallback chain. Pick
            ``self.builder_fallback_chain[ticket.dispatch_attempts %
            len(chain)]`` as the model for this dispatch, stamp a
            corresponding ``[tag:code]`` / ``[tag:strategy]`` marker on
            the title so ``dispatch.select_model`` routes correctly.
            Increment ``ticket.dispatch_attempts`` after each successful
            create_task.
          - Fix 1: stamp ``ticket.dispatched_at`` so the hard-timeout
            branch in ``_poll_children`` has a wall-clock anchor.
        """
        # Sequential-discipline Fix 2: refuse duplicate in-flight dispatch.
        # The orchestrator's existing wave-loop already guards via
        # ``_in_flight_for_wave`` (status-based), but a phantom-cleanup
        # tick can flip the ticket back to PENDING while the original
        # child is still running on the mesh — at which point the next
        # dispatch produces a sibling. The dispatch-id ledger here closes
        # that race independent of status bookkeeping.
        existing_child = self._in_flight_dispatches.get(ticket.id)
        if existing_child is not None:
            logger.warning(
                "in-flight check: %s already has child %s; "
                "skipping duplicate dispatch",
                ticket.identifier, existing_child,
            )
            self.state.record_event(
                "dispatch_idempotency_skip",
                identifier=ticket.identifier,
                existing_child_task_id=existing_child,
            )
            return

        code_key = (ticket.code or "").upper()
        if code_key:
            hint = _TARGET_HINTS.get(code_key)
            if hint is not None:
                try:
                    fresh_vr = await self._verify_hint(code_key, hint)
                    # Key parity with `_verify_wave_hints` (uppercased) AND
                    # with `_child_task_body`'s raw-`ticket.code` lookup —
                    # ticket codes are uppercase by convention, but write
                    # both keys defensively so a future lower-case code
                    # cannot silently miss the cache lookup.
                    self._verified_hints[code_key] = fresh_vr
                    if ticket.code != code_key:
                        self._verified_hints[ticket.code] = fresh_vr
                except Exception:
                    logger.exception(
                        "SAL-2787: per-dispatch re-verify crashed for %s; "
                        "falling back to wave-cached hint",
                        code_key,
                    )
        # Sequential-discipline Fix 3: pick the builder model for this
        # attempt from the fallback chain. Title-tag based routing means
        # the existing ``dispatch.select_model`` path resolves correctly
        # downstream without a schema change to the mesh task envelope.
        title = self._child_task_title(
            ticket,
            model_tag=self._builder_model_tag_for_attempt(
                ticket.dispatch_attempts
            ),
        )
        body = self._child_task_body(ticket)
        chosen_model = self.builder_fallback_chain[
            ticket.dispatch_attempts % len(self.builder_fallback_chain)
        ]
        logger.info(
            "dispatching %s %s (wave %d, epic=%s, cp=%s, "
            "attempt=%d, model=%s)",
            ticket.identifier, ticket.code, ticket.wave,
            ticket.epic, ticket.is_critical_path,
            ticket.dispatch_attempts, chosen_model,
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
        # Sequential-discipline Fix 1: stamp the dispatch wall-clock so
        # ``_poll_children``'s hard-timeout branch can fire. Use
        # ``time.time()`` (wall-clock) not ``time.monotonic()`` so a
        # restored-state ticket from a prior daemon process can compare
        # against ``ticket.dispatched_at`` consistently.
        ticket.dispatched_at = time.time()
        # Sequential-discipline Fix 2: register the in-flight child so a
        # subsequent dispatch attempt (from any code path) is refused.
        self._in_flight_dispatches[ticket.id] = ticket.child_task_id
        # Sequential-discipline Fix 3: increment the attempt counter so
        # the next dispatch (if any) lands on the next model in the chain.
        ticket.dispatch_attempts += 1
        self.state.record_event(
            "ticket_dispatched",
            identifier=ticket.identifier,
            child_task_id=ticket.child_task_id,
            attempt=ticket.dispatch_attempts,
            model=chosen_model,
        )
        # AB-17-p: successful dispatch = forward progress.
        self._last_progress_ts = time.time()

        # Linear: Todo -> In Progress via the AB-03 helper. Failure is
        # logged but non-fatal — orchestrator bookkeeping is the source of
        # truth; Linear state is a convenience mirror.
        await self._update_linear_state(ticket, "In Progress")

    def _builder_model_tag_for_attempt(self, attempt: int) -> Optional[str]:
        """Sequential-discipline Fix 3: map a dispatch attempt index to
        a ``[tag:*]`` marker that ``dispatch.select_model`` understands.

        ``select_model`` reads three patterns:
          - ``[tag:strategy]`` → ``deepseek-v3.2:cloud``
          - ``[tag:code]``     → ``qwen3-coder:480b-cloud``
          - else               → persona.preferred_model (gpt-oss:120b-cloud)

        Returns the matching tag for the model the fallback chain
        resolves to, or ``None`` when the resolved model matches the
        persona default (no override needed).
        """
        if not self.builder_fallback_chain:
            return None
        chosen = self.builder_fallback_chain[
            attempt % len(self.builder_fallback_chain)
        ]
        if chosen == "qwen3-coder:480b-cloud":
            return "[tag:code]"
        if chosen == "deepseek-v3.2:cloud":
            return "[tag:strategy]"
        return None

    def _release_in_flight_dispatch(self, ticket: Ticket) -> None:
        """Sequential-discipline Fix 2: clear the in-flight dispatch
        ledger entry for ``ticket`` once it has reached a terminal-or-
        post-build state (PR_OPEN, REVIEWING, FAILED, ESCALATED,
        MERGED_GREEN). Idempotent: a missing entry is a no-op so this
        helper is safe to call from every state-transition site.

        Called by ``_poll_children`` on every transition out of an
        active state, and by the hard-timeout branch when force-failing
        a still-active ticket.
        """
        self._in_flight_dispatches.pop(ticket.id, None)

    def _child_task_title(
        self, ticket: Ticket, model_tag: Optional[str] = None,
    ) -> str:
        # Truncate the Linear title so the full tag stays readable.
        short = (ticket.title or "")[:80].rstrip()
        code = f" {ticket.code}" if ticket.code else ""
        # Sequential-discipline Fix 3: optional model-routing tag inserted
        # before the persona tag so ``dispatch.select_model`` picks the
        # fallback chain's resolved model. ``None`` (default) preserves
        # legacy title shape exactly.
        model_prefix = f"{model_tag} " if model_tag else ""
        return (
            f"{model_prefix}[persona:alfred-coo-a] [wave-{ticket.wave}] "
            f"[{ticket.epic}] {ticket.identifier}{code} — {short}"
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
        #
        # AB-17-c (SAL — Plan I §3): pass the per-wave VerificationResult
        # through so the render can decorate the block with verified /
        # unresolved / conflict / unverified markers. AB-17-f tightened
        # this by initializing ``_verified_hints = {}`` in ``__init__``
        # (see AB-17-b block above), so no ``hasattr`` guard is needed.
        target_block = _render_target_block(
            ticket.code,
            vr=self._verified_hints.get(ticket.code),
        )
        # 2026-04-27: pre-render the canonical APE/V acceptance section
        # from the Linear ticket body into the dispatched task body. This
        # gives the builder the byte-verbatim text Hawkman validates
        # against, so Step 4(b)'s "copy verbatim into the PR body" path
        # has zero ambiguity (no http_get round-trip, no plan-doc drift).
        # 2026-04-27 deep dive showed 75% of hawkman REQUEST_CHANGES were
        # "missing APE/V citation" — primarily because builders paraphrase
        # the acceptance text. Embedding it here closes the prompt gap.
        # Best-effort: a Linear hiccup must not block dispatch, so on any
        # failure we fall back to the legacy placeholder checklist.
        apev_block = self._render_apev_acceptance_block(ticket)
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
            f"{apev_block}"
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

    def _render_apev_acceptance_block(self, ticket: Ticket) -> str:
        """Render the canonical APE/V acceptance section for a child task.

        Hawkman GATE 1 validates byte-verbatim against the Linear ticket
        body's ``## APE/V Acceptance (machine-checkable)`` section. The
        2026-04-27 deep dive (75% reject rate, dominant reason 'missing
        APE/V citation') showed that builders paraphrase the acceptance
        text when they have to fetch it themselves. Embedding the
        verbatim text directly in the dispatched task body lets the
        builder copy-paste it into the PR body with no ambiguity.

        Best-effort: a missing Linear key, transport failure, or absent
        section returns the legacy placeholder checklist so dispatch is
        never blocked.
        """
        # Lazy import — keeps test stubs isolated from tools.py.
        try:
            from alfred_coo.tools import _fetch_linear_acceptance_criteria  # noqa: WPS433
        except ImportError:
            _fetch_linear_acceptance_criteria = None  # type: ignore[assignment]

        canonical: Optional[str] = None
        if _fetch_linear_acceptance_criteria and ticket.code:
            try:
                canonical = _fetch_linear_acceptance_criteria(ticket.code)
            except Exception:  # noqa: BLE001 — best-effort, never block dispatch
                canonical = None
        # Fall back to the ticket.identifier (e.g. SAL-2641) if the code
        # field is empty but the identifier carries a SAL-NNNN string.
        if (
            canonical is None
            and _fetch_linear_acceptance_criteria
            and ticket.identifier
            and ticket.identifier != ticket.code
        ):
            try:
                canonical = _fetch_linear_acceptance_criteria(
                    ticket.identifier
                )
            except Exception:  # noqa: BLE001
                canonical = None

        if canonical and canonical.strip():
            # Embed the canonical block under the heading hawkman expects.
            # Builders are instructed (persona Step 4(b)) to copy this
            # entire section, byte-for-byte, into the PR body's first
            # top-level section.
            return (
                "## APE/V Acceptance (machine-checkable)\n"
                "(Copy this entire section, byte-for-byte, into the "
                "`body` argument of your `propose_pr`/`update_pr` call. "
                "Hawkman validates a verbatim substring match — do NOT "
                "paraphrase, reformat, or reorder.)\n"
                "\n"
                f"{canonical.strip()}\n"
            )
        # Legacy fallback: the v1-GA placeholder checklist. The persona
        # tells the builder to fetch the canonical APE/V via http_get on
        # the plan doc when this fallback fires (Step 1(b)).
        return (
            "## Acceptance (APE/V)\n"
            "- [ ] Implementation matches the plan section for this ticket.\n"
            "- [ ] Unit + integration tests added or updated.\n"
            "- [ ] `ruff` + `pytest` green in CI.\n"
            "- [ ] PR opened via `propose_pr`; orchestrator will dispatch a "
            "hawkman-qa-a review on merge-ready.\n"
            "- [ ] Structured output envelope includes the PR URL in "
            "`summary` or `follow_up_tasks`.\n"
            "(Linear ticket body had no `## APE/V Acceptance "
            "(machine-checkable)` section to pre-render — fetch it via "
            "http_get on the plan doc, then paste the verbatim text into "
            "the PR body's `## APE/V Acceptance (machine-checkable)` "
            "section per Step 4(b).)\n"
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

    async def _reconcile_orphan_active(self) -> List[Ticket]:
        """AB-17-y · force-fail tickets stuck in active state with no
        ``child_task_id``.

        The AB-17-x phantom-child reconciler inside ``_poll_children``
        is gated on ``t.child_task_id`` being truthy; an orphan-active
        ticket (active status, no child id) bypasses every recovery
        branch even though ``_in_flight_for_wave`` (status-only) keeps
        counting it as in-flight. This pre-pass closes that gap.

        Live observation (v7l, 2026-04-25): SAL-2603 (UUID 28b30b6e...)
        hydrated as ``in_progress`` from a prior daemon's persisted
        state with NO entry in ``state.dispatched_child_tasks`` across
        all 91 soul checkpoints. Watchdog reported ``in_flight=1
        ready=0`` for 70+ minutes.

        Force-fails any active-state ticket whose
        ``_ticket_transition_ts`` (last status-change clock) is older
        than ``STUCK_CHILD_FORCE_FAIL_SEC``. Sub-threshold orphans are
        intentionally tolerated so the dispatch loop has a chance to
        re-attach a child via ``_dispatch_child`` on the next tick.

        Returns the list of tickets force-failed this tick (empty if
        none) so the caller can roll them into ``_poll_children``'s
        ``updated`` set for watchdog progress accounting.
        """
        now = time.time()
        forced: List[Ticket] = []
        for ticket in self.graph.nodes.values():
            if ticket.status not in ACTIVE_TICKET_STATES:
                continue
            if ticket.child_task_id:
                # Has a child id — AB-17-x's loop will handle it. We
                # only want to catch the orphan-active class here.
                continue
            entered_ts = self._ticket_transition_ts.get(ticket.id)
            stuck_for = (now - entered_ts) if entered_ts else 0.0
            if stuck_for <= STUCK_CHILD_FORCE_FAIL_SEC:
                # Recently restored / freshly transitioned — give the
                # dispatch loop a chance to re-attach a child task.
                continue
            logger.warning(
                "AB-17-y: orphan-active %s (%s) — no child_task_id "
                "for %.0fs; force-failing",
                ticket.identifier, ticket.status.value, stuck_for,
            )
            ticket.status = TicketStatus.FAILED
            # SAL-2870 phantom-child carve-out (2026-04-26): tag the
            # failure so the retry sweep routes this STRAIGHT to PENDING.
            # Orphan-active is the daemon-restart hydration-stale class:
            # ticket has no live child + no real build attempt happened.
            # Counting this against the retry budget would burn 1 of 2
            # retries on bookkeeping for every orphan after a restart.
            ticket.last_failure_reason = "no_child_task_id"
            self.state.record_event(
                "ticket_failed",
                identifier=ticket.identifier,
                note=(
                    f"no_child_task_id: ticket in active status with "
                    f"no dispatched_child_tasks entry for "
                    f"{int(stuck_for)}s"
                ),
            )
            await self._update_linear_state(ticket, "Backlog")
            forced.append(ticket)
        return forced

    async def _poll_children(self) -> List[Ticket]:
        """Query recently completed mesh tasks and match them back to
        dispatched tickets. Returns the tickets whose statuses changed this
        tick (useful for tests + future cadence diffing).

        AB-17-x (2026-04-25, post-v7k): the poll now reconciles the
        orchestrator's internal in-flight set against mesh-state ground
        truth across THREE lifecycle states, not just ``completed``:

        - ``completed`` — child finished; extract PR URL or mark FAILED
          (existing behaviour).
        - ``failed`` — child errored externally; mark ticket FAILED with
          reason from the mesh record. Previously these were invisible
          because ``_poll_children`` only fetched ``status=completed``;
          a child that the executor marked FAILED on dispatch crash
          (main.py:585) would be a phantom.
        - ``claimed`` — child still running. Used to distinguish "really
          in flight" from "phantom" (vanished from all three lists).

        A ticket whose ``child_task_id`` is in NONE of those three lists
        AND whose status has been DISPATCHED/IN_PROGRESS for longer than
        ``STUCK_CHILD_FORCE_FAIL_SEC`` is force-failed. This breaks the
        silent-stuck loop observed on v7i (06:32 UTC) and v7k (07:14 UTC)
        where SAL-2672 SS-11's fix-round-1 child completed without a PR
        URL but its ticket never transitioned out of DISPATCHED, leaving
        ``in_flight=1 ready=0`` for hours despite zero claimed-state
        mesh tasks for the run.

        AB-17-y (2026-04-25, post-v7l, SAL-2842): a sibling reconciler
        runs BEFORE the AB-17-x in-flight filter to catch orphan-active
        tickets — a ticket whose status is in ``ACTIVE_TICKET_STATES``
        but ``child_task_id is None``. AB-17-x's filter
        (``if t.child_task_id and t.status not in TERMINAL_STATES``)
        skips these entirely, so the watchdog (status-only) sees them
        as in_flight forever. Live-observed on v7l: SAL-2603 hydrated
        in_progress from a prior daemon's checkpoint with no entry in
        ``state.dispatched_child_tasks``; ``in_flight=1 ready=0`` for
        70+ min before this fix existed. Force-fail kicks in once the
        ticket has been in its current active status for longer than
        ``STUCK_CHILD_FORCE_FAIL_SEC`` (same window as AB-17-x).
        """
        # SAL-2870 #2 · BACKED_OFF → PENDING flip-back. Runs FIRST so
        # tickets whose cooling window elapsed are visible to every
        # downstream pass in the same tick (in-flight count, ready
        # selection, dispatch). Tickets without a ``backed_off_at``
        # timestamp (shouldn't happen but defensive) are left in
        # BACKED_OFF and will be picked up next tick once their timer
        # is set by ``_back_off_ticket``.
        backed_off_woken = self._wake_backed_off_tickets()

        # SAL-2870 #3 · re-evaluate readiness on every tick. The previous
        # behaviour only unblocked downstream tickets when an upstream
        # transitioned to MERGED_GREEN inside `_select_ready`. With
        # BACKED_OFF in play, an upstream may oscillate FAILED → BACKED_OFF
        # → PENDING → IN_PROGRESS → MERGED_GREEN multiple times before
        # landing terminal, and a BLOCKED downstream needs to see the
        # latest dep snapshot every tick. Cheap, idempotent.
        self._refresh_blocked_status()

        # AB-17-y · orphan-active reconciliation (runs first so the
        # AB-17-x filter below can ignore the no-child case cleanly).
        # Force-fails any active-state ticket that's been stuck without a
        # ``child_task_id`` past the threshold; sub-threshold tickets are
        # left alone so the dispatch loop has a chance to lift them out
        # of the orphan state on the next tick. See SAL-2842 for the
        # live-observed v7l scenario this catches.
        orphan_failed = await self._reconcile_orphan_active()

        in_flight = [
            t for t in self.graph.nodes.values()
            if t.child_task_id
            and t.status not in TERMINAL_STATES
        ]
        if not in_flight:
            # SAL-2870 phantom-child carve-out (2026-04-26): orphan-only
            # short-circuit must still run the carve-out so a daemon-
            # restart wave (orphan_failed only, no in_flight) gets the
            # FAILED -> PENDING immediate re-dispatch instead of waiting
            # for the next tick to hit the main sweep below. Without this
            # the carve-out only fires when there's other in-flight work
            # — a regression from the v7ab/v7ac live-burn scenario where
            # the whole wave entry IS orphans.
            await self._apply_phantom_carve_out(orphan_failed)
            return list(orphan_failed)

        # AB-17-x: fetch all three lifecycle states in one pass so we have
        # full visibility into where each child sits on the mesh. ``failed``
        # was previously invisible; ``claimed`` lets us tell phantoms apart
        # from genuinely-running children.
        try:
            completed = await self.mesh.list_tasks(status="completed", limit=100)
        except Exception:
            logger.exception("mesh.list_tasks(completed) failed")
            return []
        try:
            failed = await self.mesh.list_tasks(status="failed", limit=100)
        except Exception:
            logger.exception("mesh.list_tasks(failed) failed; treating as empty")
            failed = []
        try:
            claimed = await self.mesh.list_tasks(status="claimed", limit=100)
        except Exception:
            logger.exception("mesh.list_tasks(claimed) failed; treating as empty")
            claimed = []

        by_id = {c.get("id"): c for c in (completed or []) if isinstance(c, dict)}
        # AB-17-x: terminal records (completed | failed) → drives state
        # transitions in this tick. Claimed IDs only feed the
        # phantom-detection branch below; we don't need their full payload.
        terminal_by_id: Dict[str, Dict[str, Any]] = dict(by_id)
        for f in (failed or []):
            if isinstance(f, dict) and f.get("id"):
                terminal_by_id[f["id"]] = f
        claimed_ids = {
            c.get("id") for c in (claimed or [])
            if isinstance(c, dict) and c.get("id")
        }
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
        now = time.time()
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
            # AB-17-x: include the mesh ``failed`` listing in the lookup so
            # an externally-failed child surfaces here. `terminal_by_id`
            # merges completed + failed.
            rec = terminal_by_id.get(ticket.child_task_id)
            if rec is None:
                # Sequential-discipline Fix 1 (2026-04-28): builder hard
                # timeout. Fires INDEPENDENT of mesh-claimed status — a
                # child that's stuck in ``claimed`` forever (gpt-oss:120b
                # silent-with-tools) is just as broken as a child that
                # vanished, and the only safe response is to force-fail
                # the ticket so the retry sweep can either back-off-and-
                # try-again on a different model or escalate to a human.
                # Distinct from AB-17-x's STUCK_CHILD_FORCE_FAIL_SEC
                # (gated on absence from claimed/completed/failed): the
                # hard-timeout uses dispatch wall-clock as its only
                # anchor, so silent-with-tools is caught.
                #
                # Tag with ``builder_hard_timeout`` (NOT in
                # ``PHANTOM_FAILURE_REASONS``) so ``_apply_phantom_carve_out``
                # routes through ``_back_off_ticket`` (consumes retry).
                # When budget is exhausted, escalate via
                # ``_escalate_builder_hard_timeout`` (mirrors the SAL-3070
                # circuit-breaker shape: ESCALATED + Backlog +
                # ``human-assigned`` + audit comment).
                if (
                    ticket.dispatched_at is not None
                    and (now - ticket.dispatched_at) > BUILDER_HARD_TIMEOUT_SEC
                ):
                    elapsed = now - ticket.dispatched_at
                    if ticket.retry_count + 1 >= ticket.retry_budget:
                        # Retry budget exhausted on this hard-timeout —
                        # escalate directly so a human gets the ticket
                        # instead of letting it land in terminal FAILED
                        # (which the wave-gate would then have to time-
                        # out separately on).
                        logger.warning(
                            "builder hard-timeout: %s dispatched %.0fs "
                            "ago with no completion; force-failing "
                            "(retry %d/%d exhausted → ESCALATED)",
                            ticket.identifier, elapsed,
                            ticket.retry_count, ticket.retry_budget,
                        )
                        ticket.status = TicketStatus.FAILED
                        ticket.last_failure_reason = "builder_hard_timeout"
                        # Consume the final retry slot so retry_count
                        # accounting matches the "retry consumed"
                        # contract. This brings retry_count up to budget,
                        # at which point _apply_phantom_carve_out's
                        # exhaustion guard would normally leave the
                        # ticket terminal-FAILED — but we route to
                        # ESCALATED instead via the dedicated helper so
                        # operators see the same Linear shape as a
                        # phantom-loop circuit-breaker fire.
                        ticket.retry_count += 1
                        self.state.record_event(
                            "ticket_failed",
                            identifier=ticket.identifier,
                            note=(
                                f"builder_hard_timeout: dispatched "
                                f"{int(elapsed)}s ago with no completion "
                                f"(retry {ticket.retry_count}/"
                                f"{ticket.retry_budget} exhausted)"
                            ),
                            reason="builder_hard_timeout",
                        )
                        await self._escalate_builder_hard_timeout(ticket)
                        self._release_in_flight_dispatch(ticket)
                        updated.append(ticket)
                        continue
                    # Budget remaining — force-fail and let the carve-out
                    # sweep route through _back_off_ticket (consume retry,
                    # transition to BACKED_OFF, schedule fresh dispatch).
                    logger.warning(
                        "builder hard-timeout: %s dispatched %.0fs ago "
                        "with no completion; force-failing "
                        "(retry consumed)",
                        ticket.identifier, elapsed,
                    )
                    ticket.status = TicketStatus.FAILED
                    ticket.last_failure_reason = "builder_hard_timeout"
                    self.state.record_event(
                        "ticket_failed",
                        identifier=ticket.identifier,
                        note=(
                            f"builder_hard_timeout: dispatched "
                            f"{int(elapsed)}s ago with no completion"
                        ),
                        reason="builder_hard_timeout",
                    )
                    await self._update_linear_state(ticket, "Backlog")
                    self._release_in_flight_dispatch(ticket)
                    updated.append(ticket)
                    continue
                # Not in completed or failed. Could be:
                #   (a) still running (in mesh ``claimed``) — normal.
                #   (b) just vanished — phantom. Force-fail after
                #       ``STUCK_CHILD_FORCE_FAIL_SEC`` of no transition.
                in_claimed = ticket.child_task_id in claimed_ids
                if in_claimed:
                    # Healthy in-flight; bump DISPATCHED → IN_PROGRESS.
                    if ticket.status == TicketStatus.DISPATCHED:
                        ticket.status = TicketStatus.IN_PROGRESS
                    continue
                # Phantom: not claimed, not completed, not failed. Apply
                # the time-based escape hatch. Use _ticket_transition_ts
                # populated by `_snapshot_graph_into_state` as the
                # "entered current status" reference — this is the most
                # reliable per-ticket clock the orchestrator already
                # maintains for the stall watcher.
                entered_ts = self._ticket_transition_ts.get(ticket.id)
                stuck_for = (now - entered_ts) if entered_ts else 0.0
                if stuck_for > STUCK_CHILD_FORCE_FAIL_SEC:
                    logger.warning(
                        "AB-17-x: phantom child %s for %s (%s) — not in "
                        "claimed/completed/failed for %.0fs; force-failing",
                        ticket.child_task_id, ticket.identifier,
                        ticket.status.value, stuck_for,
                    )
                    ticket.status = TicketStatus.FAILED
                    # SAL-2870 phantom-child carve-out (2026-04-26): tag the
                    # failure so the retry sweep below routes this STRAIGHT
                    # to PENDING instead of through BACKED_OFF + retry bump.
                    # Phantom cleanup is bookkeeping, not a real build fail.
                    ticket.last_failure_reason = "phantom_child"
                    self.state.record_event(
                        "ticket_failed",
                        identifier=ticket.identifier,
                        note=(
                            f"phantom_child: child_task_id="
                            f"{ticket.child_task_id} not in mesh "
                            f"claimed/completed/failed for "
                            f"{int(stuck_for)}s"
                        ),
                    )
                    await self._update_linear_state(ticket, "Backlog")
                    updated.append(ticket)
                    continue
                # Below the threshold: leave alone for now (a brief
                # mesh inconsistency between PATCH /complete and the
                # next ?status=completed query is normal — sub-second
                # in practice but bounded by soul-svc's read-after-
                # write semantics). Bump DISPATCHED→IN_PROGRESS so a
                # status snapshot can be taken.
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
            # SAL-2886: distinguish the two no-PR completion shapes BEFORE
            # falling through to the silent-bug FAILED branch.
            #
            # Persona contract (persona.py:58-67): the alfred-coo-a builder
            # emits exactly one of (a) propose_pr -> PR URL, (b)
            # linear_create_issue -> grounding-gap issue id. Mode (b) is the
            # documented response to "(conflict ...)" / "(unresolved ...)"
            # markers in the rendered ## Target block - itself produced by
            # _verify_hint flagging HintStatus.PATH_CONFLICT for new_paths
            # that already exist on main (i.e. the ticket's work was merged
            # in a prior wave/run).
            #
            # Treat that as terminal-non-failure: ESCALATED. Wave-gate
            # already excuses PATH_CONFLICT from the green ratio
            # (_is_wave_gate_excused); ESCALATED is the per-ticket terminal
            # that mirrors that wave-level excusal so the retry-budget sweep
            # below does NOT route this ticket through BACKED_OFF and burn
            # retries (v7p signature).
            if not pr_url and self._envelope_is_grounding_gap(result):
                ticket.status = TicketStatus.ESCALATED
                grounding_gap_ident = self._envelope_grounding_gap_identifier(
                    result
                )
                self.state.record_event(
                    "ticket_escalated",
                    identifier=ticket.identifier,
                    grounding_gap=grounding_gap_ident,
                )
                # SAL-2893 (original): transitioned Linear to "Done" with a
                # grounding-gap link so ``build_ticket_graph`` would not
                # re-read it as ``in_progress`` and AB-17-y would not catch
                # it on the orphan-active sweep next kickoff. PR #91 had left
                # Linear untouched on ESCALATED, which burned 30 min per
                # kickoff on the same stale ticket.
                #
                # 2026-04-27 follow-up (this fix): "Done" was the wrong
                # target. It falsely claimed the ticket had shipped — on
                # 2026-04-27 four tickets in the MC v1 GA project (SAL-2597,
                # SAL-2627, SAL-2668, SAL-2677) were marked Done with no
                # merged PR because they hit this branch. Backlog also
                # escapes the orphan-active sweep (which only scans In
                # Progress, see ``_sweep_orphan_active``), without
                # misrepresenting the ticket as shipped. We additionally
                # apply the ``human-assigned`` label so the dispatch-gate
                # fix (fix/dispatch-gate-human-assigned-AB-17-v) excuses
                # the ticket on the next kickoff until a human resolves
                # the grounding gap — preventing a re-dispatch loop where
                # the same gap surfaces every kickoff.
                #
                # Idempotency guard: skip the write + comment + label if
                # Linear is already in a non-active state (operator already
                # closed it manually, or this is re-entry from rehydrated
                # state). "Done" stays in the guard list because historical
                # mis-Done tickets — produced by the pre-fix branch — must
                # not be re-touched and re-commented on every poll tick.
                already_terminal = (ticket.linear_state or "").strip().lower() in (
                    "backlog",
                    "done", "merged", "released", "completed",
                    "canceled", "cancelled", "duplicate",
                )
                if not already_terminal:
                    await self._update_linear_state(ticket, "Backlog")
                    await self._apply_linear_label(ticket, "human-assigned")
                    await self._post_escalated_linear_comment(
                        ticket, grounding_gap_ident
                    )
                    ticket.linear_state = "Backlog"
                updated.append(ticket)
                continue
            if pr_url:
                ticket.pr_url = pr_url
                ticket.status = TicketStatus.PR_OPEN
                # Sequential-discipline Fix 1: build phase is done; clear
                # the dispatch wall-clock so a hypothetical fix-round
                # dispatch starts with a fresh hard-timeout window.
                ticket.dispatched_at = None
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
                #
                # SAL-2978 (2026-04-25): split the FAILED reason into two
                # explicit shapes so ops can grep + the wave-gate math
                # cannot mistake either for a happy-path completion:
                #   (a) silent_complete: empty summary + no PR + no
                #       follow-up + no grounding-gap artifact, OR a
                #       truncated tool-loop envelope. Defense-in-depth
                #       backstop for the iteration-cap fix in main.py.
                #   (b) no_pr_url: builder produced *some* envelope content
                #       but no PR URL — model called the wrong tool, or
                #       summarised work without committing it.
                if self._envelope_is_silent_complete(result):
                    failure_reason = "silent_complete"
                    failure_note = (
                        "envelope rejected: silent-complete shape "
                        "(empty summary + no PR + no follow-up). "
                        "Likely tool-loop hit MAX_TOOL_ITERATIONS or "
                        "model returned without calling propose_pr."
                    )
                    logger.error(
                        "SAL-2978: %s envelope rejected as silent_complete; "
                        "result_keys=%s truncated=%s",
                        ticket.identifier,
                        list(result.keys()) if isinstance(result, dict) else "non-dict",
                        result.get("truncated") if isinstance(result, dict) else None,
                    )
                else:
                    failure_reason = "no_pr_url"
                    failure_note = "child completed without PR URL"
                ticket.status = TicketStatus.FAILED
                self.state.record_event(
                    "ticket_failed",
                    identifier=ticket.identifier,
                    note=failure_note,
                    reason=failure_reason,
                )
                # Sub #62 (2026-04-27): record a hard-timeout against the
                # builder role on silent_complete. Three in a row trips the
                # auto-rollback to stable_baseline. Best-effort: registry
                # mishaps must not break the existing failure path.
                if failure_reason == "silent_complete":
                    try:
                        from .model_registry import record_hard_timeout
                        crossed = record_hard_timeout("builder")
                        if crossed:
                            try:
                                await self.cadence.post(
                                    f":rotating_light: model-registry: "
                                    f"builder role auto-rolled back to "
                                    f"stable_baseline after 3 consecutive "
                                    f"silent_complete failures (last ticket "
                                    f"{ticket.identifier})."
                                )
                            except Exception:  # noqa: BLE001
                                logger.exception(
                                    "cadence.post(auto_rollback) failed; "
                                    "continuing"
                                )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "model_registry.record_hard_timeout failed; "
                            "auto-rollback may be skipped"
                        )
                await self._update_linear_state(ticket, "Backlog")
                updated.append(ticket)

        # AB-17-y: roll orphan-active force-fails into the same updated
        # list so the watchdog sees them as forward progress and the
        # caller (cadence diff, tests) gets a unified ticket list.
        if orphan_failed:
            updated.extend(orphan_failed)

        # SAL-2870 #1 · retry-budget sweep. Every ticket that just
        # transitioned to FAILED in this tick (caught via the `updated`
        # list) is reconsidered: if it has retry budget left, flip back
        # to BACKED_OFF and clear ``child_task_id`` so the next dispatch
        # creates a fresh child. Sweep runs AFTER all the existing
        # FAILED-transition branches so it's surgical: every prior
        # codepath that wrote ``ticket.status = FAILED`` still works,
        # we just re-route the verdict at the end. This minimizes
        # conflict surface with the parallel SAL-2869 sub.
        #
        # SAL-2870 phantom-child carve-out (2026-04-26): see
        # ``_apply_phantom_carve_out`` — phantom_child / no_child_task_id
        # tickets short-circuit BACKED_OFF + retry-count, going straight
        # to PENDING for immediate re-dispatch. Same helper is invoked
        # by the orphan-only short-circuit above.
        await self._apply_phantom_carve_out(updated)

        # SAL-2870: also bake in BACKED_OFF wake-ups + dep refreshes as
        # forward-progress markers so the deadlock-grace timer resets
        # whenever there's any motion at all in the graph.
        if backed_off_woken:
            updated.extend(backed_off_woken)

        # AB-17-p: any state transition this tick (PR_OPEN, REVIEWING,
        # FAILED) counts as forward progress. Single stamp at the loop
        # exit keeps this cheap and covers every branch above.
        if updated:
            self._last_progress_ts = time.time()

        # Sequential-discipline Fix 2 (2026-04-28): garbage-collect the
        # in-flight dispatch ledger. The ledger guards the BUILD phase
        # (DISPATCHED + IN_PROGRESS); any ticket whose status has moved
        # past those — to PR_OPEN, REVIEWING, MERGE_REQUESTED, FAILED,
        # ESCALATED, BACKED_OFF, PENDING, or terminal MERGED_GREEN — has
        # its dispatch slot released so a future re-dispatch (after
        # retry / phantom-cleanup / wake-from-backoff / fix-round) is
        # not blocked by a stale ledger entry. Run as a sweep at
        # end-of-poll so every transition branch above — current and
        # future — is automatically covered without each branch having
        # to remember to call ``_release_in_flight_dispatch`` manually.
        _BUILD_PHASE = frozenset({
            TicketStatus.DISPATCHED,
            TicketStatus.IN_PROGRESS,
        })
        for tid in list(self._in_flight_dispatches.keys()):
            t = self.graph.nodes.get(tid)
            if t is None or t.status not in _BUILD_PHASE:
                self._in_flight_dispatches.pop(tid, None)
        return updated

    def _wake_backed_off_tickets(self) -> List[Ticket]:
        """SAL-2870 #2: scan every ticket in ``BACKED_OFF`` and flip back
        to ``PENDING`` if ``time.time() - backed_off_at >= retry_backoff_sec``.
        Returns the list of tickets woken this tick (used by the caller as
        a forward-progress signal so the deadlock-grace timer resets).
        Tickets with no ``backed_off_at`` (defensive: shouldn't happen) are
        left alone — the next tick of ``_back_off_ticket`` will populate
        the timestamp.
        """
        if self.retry_backoff_sec <= 0:
            # Zero/negative backoff means no cooling window — flip back
            # immediately. Useful for tests + dry-run.
            woken: List[Ticket] = []
            for ticket in self.graph.nodes.values():
                if ticket.status == TicketStatus.BACKED_OFF:
                    ticket.status = TicketStatus.PENDING
                    ticket.backed_off_at = None
                    woken.append(ticket)
                    self.state.record_event(
                        "ticket_woke_from_backoff",
                        identifier=ticket.identifier,
                        retry_count=ticket.retry_count,
                    )
            return woken
        now = time.time()
        woken = []
        for ticket in self.graph.nodes.values():
            if ticket.status != TicketStatus.BACKED_OFF:
                continue
            if ticket.backed_off_at is None:
                continue
            elapsed = now - ticket.backed_off_at
            if elapsed >= self.retry_backoff_sec:
                logger.info(
                    "SAL-2870: %s woken from BACKED_OFF after %.0fs "
                    "(retry %d/%d)",
                    ticket.identifier, elapsed,
                    ticket.retry_count, ticket.retry_budget,
                )
                ticket.status = TicketStatus.PENDING
                ticket.backed_off_at = None
                self.state.record_event(
                    "ticket_woke_from_backoff",
                    identifier=ticket.identifier,
                    retry_count=ticket.retry_count,
                    elapsed_sec=int(elapsed),
                )
                woken.append(ticket)
        return woken

    def _refresh_blocked_status(self) -> None:
        """SAL-2870 #3: re-walk every BLOCKED ticket and downgrade to
        PENDING when its deps are now satisfied. The existing
        ``_select_ready`` already does this, but only for tickets being
        considered for dispatch in *this* call. With retry semantics in
        play (an upstream FAILED → BACKED_OFF → re-dispatched →
        MERGED_GREEN), a BLOCKED downstream that wasn't in the candidate
        list when its upstream was FAILED could miss the unblock.
        Idempotent + cheap (single pass over the graph; no I/O).

        Symmetric path: a PENDING ticket whose deps are not yet satisfied
        is moved to BLOCKED, which keeps the cadence display honest.
        Tickets in active or terminal states are not touched.
        """
        for ticket in self.graph.nodes.values():
            if ticket.status == TicketStatus.BLOCKED:
                if self._deps_satisfied(ticket):
                    logger.debug(
                        "SAL-2870: %s deps now satisfied; "
                        "BLOCKED → PENDING (retry-aware unblock)",
                        ticket.identifier,
                    )
                    ticket.status = TicketStatus.PENDING
                    self.state.record_event(
                        "ticket_unblocked",
                        identifier=ticket.identifier,
                    )
            elif ticket.status == TicketStatus.PENDING:
                # If a previously-PENDING ticket's upstream just FAILED →
                # BACKED_OFF (waiting to retry), it should display as
                # BLOCKED until the upstream lands MERGED_GREEN. Without
                # this flip the cadence + deadlock detector see it as
                # ready-but-uncalled which is misleading.
                if not self._deps_satisfied(ticket):
                    ticket.status = TicketStatus.BLOCKED

    async def _apply_phantom_carve_out(self, tickets: List[Ticket]) -> None:
        """SAL-2870 retry-budget sweep + phantom-child carve-out
        (2026-04-26). For each ticket in ``tickets`` that currently sits
        in ``FAILED``:

        - If it was tagged ``phantom_child`` (AB-17-x) or
          ``no_child_task_id`` (AB-17-y) AND retry is enabled
          (``retry_budget > 0``): consult the per-ticket consecutive-
          phantom circuit breaker (SAL-3070 / SAL-3038, 2026-04-28).
          - If ≥``PHANTOM_LOOP_MAX_IN_WINDOW`` phantoms have fired for
            this ticket inside the trailing ``PHANTOM_LOOP_WINDOW_SEC``
            window, force-escalate (ESCALATED + Linear Backlog +
            ``human-assigned`` label + audit comment) and break the
            cycle. The ticket is still removed from any retry pressure.
          - Otherwise (1-3 phantoms in 10 min — legitimate transient
            flake): short-circuit BACKED_OFF, route straight to PENDING
            via ``_reset_phantom_failure``. Skips the retry_count bump —
            phantom cleanup is bookkeeping, not a real build attempt.
        - Otherwise (real failure with budget remaining): route through
          BACKED_OFF + retry_count++ via ``_back_off_ticket``.
        - Otherwise (real failure with budget exhausted, OR phantom
          with retry disabled): leave terminal FAILED.

        Real failures still carry the old (silent_complete / no_pr_url /
        mesh-failed / review-changes) BACKED_OFF cooling semantics. The
        carve-out is gated on ``retry_budget > 0`` so legacy tests +
        operators that pin ``retry_budget=0`` keep terminal-FAILED.

        Async because the SAL-3070 circuit-breaker escalation path
        invokes ``_update_linear_state`` / ``_apply_linear_label`` /
        ``_post_escalated_linear_comment`` which all hit the Linear API.
        Both call sites in ``_poll_children`` are already in an async
        context so the await is free.
        """
        for ticket in list(tickets):
            if ticket.status != TicketStatus.FAILED:
                continue
            # Phantom carve-out — only when retry is enabled.
            if (
                ticket.retry_budget > 0
                and ticket.last_failure_reason in PHANTOM_FAILURE_REASONS
            ):
                # SAL-3070 / SAL-3038 (2026-04-28) consecutive-phantom
                # circuit breaker. Track + prune BEFORE deciding whether
                # to escalate so legitimate transient flakes (1-3 in
                # 10 min) still go through the unchanged carve-out.
                if self._phantom_loop_breaker_tripped(ticket):
                    await self._escalate_phantom_loop(ticket)
                    continue
                self._reset_phantom_failure(ticket)
                continue
            if ticket.retry_count >= ticket.retry_budget:
                continue
            self._back_off_ticket(ticket)

    def _phantom_loop_breaker_tripped(self, ticket: Ticket) -> bool:
        """SAL-3070 / SAL-3038 (2026-04-28) consecutive-phantom circuit
        breaker bookkeeping. Appends ``time.monotonic()`` to the per-
        ticket history, prunes entries older than
        ``PHANTOM_LOOP_WINDOW_SEC``, and returns True iff the post-prune
        history size is ≥ ``PHANTOM_LOOP_MAX_IN_WINDOW`` — the trip
        condition. Mutating method by design: every phantom-cleanup that
        reaches the carve-out branch must record an entry, even when the
        breaker doesn't trip, so the count is accurate on the next call.
        """
        now = time.monotonic()
        history = self._consecutive_phantoms.setdefault(ticket.id, [])
        history.append(now)
        cutoff = now - PHANTOM_LOOP_WINDOW_SEC
        # Prune in-place so future calls see only the trailing window.
        pruned = [ts for ts in history if ts >= cutoff]
        self._consecutive_phantoms[ticket.id] = pruned
        return len(pruned) >= PHANTOM_LOOP_MAX_IN_WINDOW

    async def _escalate_phantom_loop(self, ticket: Ticket) -> None:
        """SAL-3070 / SAL-3038 (2026-04-28) circuit-breaker escalation.

        Mirrors the SAL-2886 / SAL-2893 escalate-path exactly so an
        operator handling a phantom-loop ticket sees the same Linear
        state + label + comment as a grounding-gap escalation:

          1. ``ticket.status = ESCALATED`` (terminal-non-failure;
             wave-gate excludes from numerator + denominator).
          2. Linear state → ``Backlog`` (escapes orphan-active sweep
             without falsely claiming the ticket shipped).
          3. Linear label → ``human-assigned`` (dispatch-gate excuses
             the ticket on the next kickoff until a human resolves).
          4. Linear comment via ``_post_escalated_linear_comment`` with
             grounding_gap_ident=``"phantom-loop-circuit-breaker"`` so
             the audit trail captures why this ticket was force-failed.
          5. ``self._consecutive_phantoms[ticket.id]`` reset so re-entry
             from a daemon restart starts fresh.
          6. Skip the normal retry-fresh-dispatch — break out of the
             cleanup branch.

        Best-effort: if any Linear write raises the helpers themselves
        log + swallow; the orchestrator's graph stays source of truth
        and the ESCALATED status alone is enough to lift the wave-gate.
        """
        n_in_window = len(self._consecutive_phantoms.get(ticket.id, []))
        ticket.status = TicketStatus.ESCALATED
        # Clear in-flight bookkeeping so the next dispatch path (if any)
        # cannot re-attach to a stale child. ESCALATED is terminal so
        # this is mostly defensive.
        ticket.child_task_id = None
        ticket.last_failure_reason = None
        logger.warning(
            "phantom-loop circuit breaker: %s escalated after %d "
            "consecutive phantoms in %dmin window",
            ticket.identifier,
            n_in_window,
            PHANTOM_LOOP_WINDOW_SEC // 60,
        )
        self.state.record_event(
            "ticket_escalated",
            identifier=ticket.identifier,
            grounding_gap="phantom-loop-circuit-breaker",
            phantom_count=n_in_window,
            window_sec=PHANTOM_LOOP_WINDOW_SEC,
        )
        # Idempotency guard mirrors the SAL-2893 escalate-path: skip the
        # write + label + comment if Linear is already in a non-active
        # state (operator already closed it manually, or this is re-
        # entry from rehydrated state).
        already_terminal = (ticket.linear_state or "").strip().lower() in (
            "backlog",
            "done", "merged", "released", "completed",
            "canceled", "cancelled", "duplicate",
        )
        if not already_terminal:
            await self._update_linear_state(ticket, "Backlog")
            await self._apply_linear_label(ticket, "human-assigned")
            await self._post_escalated_linear_comment(
                ticket,
                grounding_gap_ident="phantom-loop-circuit-breaker",
            )
            ticket.linear_state = "Backlog"
        # Reset the counter so a future re-entry starts fresh. The
        # ESCALATED terminal status already prevents re-dispatch on
        # this run; clearing the history matters only if an operator
        # reopens the ticket in Linear and the daemon picks it up
        # again on a later kickoff.
        self._consecutive_phantoms.pop(ticket.id, None)

    async def _escalate_builder_hard_timeout(self, ticket: Ticket) -> None:
        """Sequential-discipline Fix 1 (2026-04-28). Force-escalate a
        ticket whose builder hard-timeout fired with retry budget already
        exhausted. Mirrors ``_escalate_phantom_loop`` shape so an
        operator handling a hard-timeout sees the same Linear surface as
        a phantom-loop or wave-stall escalation:

          1. ``ticket.status = ESCALATED`` (terminal-non-failure).
          2. Linear → ``Backlog`` (escapes orphan-active sweep without
             falsely claiming the ticket shipped).
          3. Linear label → ``human-assigned`` (dispatch-gate excuses
             the ticket on the next kickoff).
          4. Linear comment with grounding_gap_ident=
             ``"builder-hard-timeout"`` so the audit trail captures
             *why* the ticket was force-failed (distinct from the
             phantom-loop and wave-stall idents).

        Idempotency guard mirrors the SAL-2893 escalate-path: skip the
        write + label + comment if Linear is already in a non-active
        state (operator already closed it manually, or this is re-entry
        from rehydrated state).
        """
        ticket.status = TicketStatus.ESCALATED
        ticket.last_failure_reason = None
        # Drop child task id since we're abandoning the dispatch entirely;
        # ESCALATED is terminal so re-attach is impossible on this run.
        ticket.child_task_id = None
        ticket.dispatched_at = None
        already_terminal = (ticket.linear_state or "").strip().lower() in (
            "backlog",
            "done", "merged", "released", "completed",
            "canceled", "cancelled", "duplicate",
        )
        if not already_terminal:
            await self._update_linear_state(ticket, "Backlog")
            await self._apply_linear_label(ticket, "human-assigned")
            await self._post_escalated_linear_comment(
                ticket,
                grounding_gap_ident="builder-hard-timeout",
            )
            ticket.linear_state = "Backlog"

    async def _maybe_force_pass_stalled_wave(
        self,
        wave_n: int,
        wave_tickets: List[Ticket],
    ) -> bool:
        """SAL-3070 / SAL-3038 wave-gate stall force-pass (2026-04-28).

        Tracks per-wave ``(monotonic_ts, green_count)`` history bounded
        to the last 20 entries. If the trailing
        ``WAVE_STALL_FORCE_PASS_SEC`` window contains no increase in
        ``green_count`` AND the window is fully populated (the earliest
        entry inside the window is at least ``WAVE_STALL_FORCE_PASS_SEC``
        old), force-pass the wave: every non-terminal ticket is escalated
        with the same Linear-state + label + comment shape as the SAL-
        3070 phantom-loop circuit breaker so an operator sees a clear
        ``human-assigned`` handoff. Returns True iff the wave was force-
        passed (caller breaks the dispatch loop); False if the wave is
        still making progress or hasn't accumulated enough history yet.

        A wave whose ``green_count`` is still rising is never force-
        passed — the moment the latest sample exceeds the earliest
        in-window sample, the timer is implicitly reset by the
        comparison. Only genuine stall-passes fire the helper.
        """
        now = time.monotonic()
        green_count = sum(
            1 for t in wave_tickets if t.status == TicketStatus.MERGED_GREEN
        )
        history = self._wave_progress_history.setdefault(wave_n, [])
        history.append((now, green_count))
        # Cap at the last 20 entries.
        if len(history) > 20:
            del history[: len(history) - 20]

        # Stall criterion: there exists a sample at or before
        # ``now - WAVE_STALL_FORCE_PASS_SEC`` whose green_count is ≥
        # the current green_count (and therefore equal, since later
        # samples must be ≥ earlier ones in monotonic green-count
        # accounting). Equivalently: somewhere ≥30min ago we observed
        # the same green_count we have now → no progress in 30min.
        #
        # We DON'T prune samples older than the cutoff — we need them
        # specifically to declare a stall. The 20-entry cap bounds
        # memory; at a 45s poll cadence that's ~15min of history per
        # wave, but combined with the earliest-old anchor it's enough
        # to detect a flat 30-min span.
        cutoff = now - WAVE_STALL_FORCE_PASS_SEC
        # Find the youngest sample at-or-before the cutoff (i.e., the
        # most recent sample that's ≥ WAVE_STALL_FORCE_PASS_SEC old).
        # If no such sample exists we haven't observed enough history
        # yet — early-tick noise must not trip the breaker.
        anchor = None
        for ts, gc in history:
            if ts <= cutoff:
                anchor = (ts, gc)  # keep latest such sample
            else:
                break
        if anchor is None:
            return False

        anchor_gc = anchor[1]
        # If green_count has risen since the anchor, the wave is making
        # progress — let it continue naturally. (Strict >: equality is
        # the stall signature; a single-step rise lifts the breaker.)
        if green_count > anchor_gc:
            return False

        # Stall confirmed. Force-pass.
        stalled_for_sec = int(now - anchor[0])
        non_terminal = [
            t for t in wave_tickets if t.status not in TERMINAL_STATES
        ]
        logger.warning(
            "wave %d stalled: green_count=%d unchanged for %dmin; "
            "force-passing wave",
            wave_n, green_count,
            WAVE_STALL_FORCE_PASS_SEC // 60,
        )
        self.state.record_event(
            "wave_force_passed_stalled",
            wave=wave_n,
            green_count=green_count,
            stalled_for_sec=stalled_for_sec,
            escalated=[t.identifier for t in non_terminal],
        )
        for ticket in non_terminal:
            await self._escalate_stalled_wave_ticket(ticket, wave_n)
        # Reset the wave's history so a re-entry on the next kickoff
        # doesn't immediately re-trip on stale entries.
        self._wave_progress_history[wave_n] = []
        return True

    async def _escalate_stalled_wave_ticket(
        self, ticket: Ticket, wave_n: int,
    ) -> None:
        """SAL-3070 / SAL-3038 wave-stall escalation (2026-04-28).

        Mirrors ``_escalate_phantom_loop`` exactly except for the
        ``grounding_gap_ident`` audit string. Kept as a sibling helper
        so the two circuit-breaker paths can evolve independently
        without fighting over a single super-helper's signature.
        """
        ticket.status = TicketStatus.ESCALATED
        ticket.child_task_id = None
        ticket.last_failure_reason = None
        self.state.record_event(
            "ticket_escalated",
            identifier=ticket.identifier,
            grounding_gap="wave-stall-force-pass",
            wave=wave_n,
        )
        already_terminal = (ticket.linear_state or "").strip().lower() in (
            "backlog",
            "done", "merged", "released", "completed",
            "canceled", "cancelled", "duplicate",
        )
        if not already_terminal:
            await self._update_linear_state(ticket, "Backlog")
            await self._apply_linear_label(ticket, "human-assigned")
            await self._post_escalated_linear_comment(
                ticket,
                grounding_gap_ident="wave-stall-force-pass",
            )
            ticket.linear_state = "Backlog"

    def _back_off_ticket(self, ticket: Ticket) -> None:
        """SAL-2870 #1: route a FAILED ticket through BACKED_OFF when it
        still has retry budget. Increments ``retry_count``, sets the
        cooling timestamp, clears ``child_task_id`` so the next dispatch
        spawns a fresh sub. Caller has already verified
        ``retry_count < retry_budget``.
        """
        ticket.retry_count += 1
        ticket.status = TicketStatus.BACKED_OFF
        ticket.backed_off_at = time.time()
        # Clear in-flight bookkeeping so the next dispatch creates a fresh
        # child. Leave ``pr_url`` + ``review_cycles`` alone — those carry
        # forward across retries (a fix-round dispatch may legitimately
        # update the same PR rather than open a new one).
        ticket.child_task_id = None
        # Sequential-discipline Fix 1 + Fix 2 (2026-04-28): clear the
        # dispatch wall-clock + release the in-flight dispatch ledger so
        # the next dispatch starts cleanly with a fresh ``dispatched_at``
        # and the idempotency guard accepts the new attempt.
        ticket.dispatched_at = None
        self._release_in_flight_dispatch(ticket)
        # SAL-2870 phantom-child carve-out (2026-04-26): ensure the
        # phantom tag doesn't leak into a real BACKED_OFF cycle. The
        # carve-out path in the sweep already routes phantom-tagged
        # failures away from this function, so reaching here means the
        # tag (if any) is from a stale prior round; clear it.
        ticket.last_failure_reason = None
        logger.warning(
            "SAL-2870: %s FAILED but retry %d/%d available; "
            "→ BACKED_OFF for %ds",
            ticket.identifier,
            ticket.retry_count, ticket.retry_budget,
            self.retry_backoff_sec,
        )
        self.state.record_event(
            "ticket_backed_off",
            identifier=ticket.identifier,
            retry_count=ticket.retry_count,
            retry_budget=ticket.retry_budget,
            backoff_sec=self.retry_backoff_sec,
        )

    def _reset_phantom_failure(self, ticket: Ticket) -> None:
        """SAL-2870 phantom-child carve-out (2026-04-26): undo a
        bookkeeping-only force-fail so the ticket can be re-dispatched
        on the very next tick with no penalty.

        Called by the retry sweep when ``ticket.last_failure_reason`` is
        in ``PHANTOM_FAILURE_REASONS`` (currently ``phantom_child`` for
        AB-17-x and ``no_child_task_id`` for AB-17-y). Differs from
        ``_back_off_ticket`` in three places:

        - status flips FAILED → PENDING (NOT → BACKED_OFF), so the
          dispatch loop picks the ticket up on the same tick a sweep
          would have left it cooling for ``retry_backoff_sec``.
        - ``retry_count`` is NOT incremented. Phantom cleanup is not a
          real build attempt; the operator's 2-retry budget should not
          be burned on hydration-stale state from a daemon restart.
        - ``last_failure_reason`` is cleared so the carve-out only
          applies to the failure it was tagged for; a subsequent real
          failure will route normally through BACKED_OFF.

        ``child_task_id`` is cleared (same as BACKED_OFF) so the next
        dispatch spawns a fresh sub with a new mesh task id.

        Live evidence (2026-04-26 v7ac wave-2): 4 phantom orphans each
        burned 300s + dispatch-time ~5+ min idle x 4 = ~20 min before
        any productive work could resume. With this carve-out the same
        4 cleanups dispatch immediately on the next tick.
        """
        ticket.status = TicketStatus.PENDING
        ticket.backed_off_at = None
        # Clear in-flight bookkeeping so the next dispatch creates a fresh
        # child. Mirrors _back_off_ticket so the dispatch path can't see
        # a stale id and re-trip phantom detection on the next tick.
        ticket.child_task_id = None
        # Sequential-discipline Fix 1 + Fix 2 (2026-04-28): also clear
        # ``dispatched_at`` and release the in-flight ledger so the
        # immediate fresh dispatch the carve-out enables is not blocked
        # by either guard. ``dispatch_attempts`` is intentionally NOT
        # reset — the fallback chain should still rotate on the next
        # attempt even after a phantom-cleanup, since the phantom may
        # itself be a symptom of the model the previous attempt used.
        ticket.dispatched_at = None
        self._release_in_flight_dispatch(ticket)
        reason = ticket.last_failure_reason
        ticket.last_failure_reason = None
        logger.warning(
            "[SAL-2870] %s phantom-child cleanup (reason=%s); "
            "skipping BACKED_OFF, dispatching fresh immediately "
            "(retry %d/%d unchanged)",
            ticket.identifier, reason,
            ticket.retry_count, ticket.retry_budget,
        )
        self.state.record_event(
            "ticket_phantom_reset",
            identifier=ticket.identifier,
            reason=reason or "",
            retry_count=ticket.retry_count,
            retry_budget=ticket.retry_budget,
        )

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

    @staticmethod
    def _envelope_is_silent_complete(result: Dict[str, Any]) -> bool:
        """SAL-2978 (2026-04-25): True iff the child's result envelope shows
        the documented "silent-complete" shape — empty summary AND no PR URL
        AND no documented escalate-path artifact. This is the failure mode
        v7aa observed on SAL-2588 TIR-06: the model exhausted its iteration
        cap mid-tool-chain, the dispatcher returned a partial envelope with
        no summary / no PR / no follow-up, and ``_poll_children`` marked the
        ticket FAILED with the generic "child completed without PR URL"
        reason.

        This helper lets the FAILED branch raise a *specific* error that
        ops can grep for and that the wave-gate math cannot mistake for
        a happy-path completion. Defense-in-depth: the iteration-cap fix
        in `main._builder_iteration_cap` should prevent most of these, but
        an explicit shape rejection here means any silent-complete envelope
        that still slips through gets flagged loudly.

        Conservative — requires ALL THREE conditions:
          1. ``summary`` is missing or empty/whitespace-only
          2. No PR URL extractable from the envelope (already None at the
             call site, so callers should pass ``has_pr_url=False``)
          3. The envelope is NOT a grounding-gap escalation (those have
             a valid ``linear_create_issue`` tool call and are handled
             by ``_envelope_is_grounding_gap`` upstream)

        Truncation marker (``"truncated": True``) is also a strong
        positive signal — a truncated tool-loop envelope is a silent-
        complete by definition.
        """
        if not isinstance(result, dict):
            # An entirely missing / non-dict result is the worst silent
            # case; treat it as silent-complete so it gets rejected.
            return True
        # Truncated envelopes from `_tool_loop` exhausting the cap. The
        # dispatcher's partial-return shape is unmistakable.
        if result.get("truncated") is True:
            return True
        summary = result.get("summary")
        content = result.get("content")
        # Either field can carry the textual summary depending on persona.
        text = ""
        if isinstance(summary, str):
            text = summary
        elif isinstance(content, str):
            text = content
        if text.strip():
            return False
        # Empty summary AND no follow-up artifacts means there's nothing
        # actionable in the envelope. (The grounding-gap check is done
        # upstream by `_envelope_is_grounding_gap` so we don't re-check
        # it here; callers must run that first.)
        follow = result.get("follow_up_tasks") or []
        artifacts = result.get("artifacts") or []
        if isinstance(follow, list) and follow:
            return False
        if isinstance(artifacts, list) and artifacts:
            return False
        return True

    @staticmethod
    def _envelope_is_grounding_gap(result: Dict[str, Any]) -> bool:
        """SAL-2886: True iff the child's result envelope shows the
        documented escalate-path emit (persona.py:58-67 / Step 0):
        a single ``linear_create_issue`` tool call that returned a
        Linear issue identifier whose title starts with
        ``"grounding gap"``. Conservative - requires BOTH the tool name
        and a recognisable grounding-gap issue identifier in the call's
        result so an unrelated linear_create_issue (e.g. a side-effect
        from Step 4 plan-doc work that ALSO produced a PR) does not
        match. Mode (a) propose_pr emits put the PR URL in
        ``summary``/``follow_up_tasks``/``tool_calls[*].result.pr_url``;
        ``_extract_pr_url`` already covers that, so this helper is only
        consulted when ``_extract_pr_url`` returned None.
        """
        if not isinstance(result, dict):
            return False
        tool_calls = result.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            return False
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            if call.get("name") != "linear_create_issue":
                continue
            out = call.get("result")
            if isinstance(out, str):
                try:
                    out = json.loads(out)
                except (ValueError, TypeError):
                    continue
            if not isinstance(out, dict):
                continue
            title = (out.get("title") or "").lower()
            ident = out.get("identifier") or ""
            if ident and ("grounding gap" in title or "grounding-gap" in title):
                return True
        return False

    @staticmethod
    def _envelope_grounding_gap_identifier(
        result: Dict[str, Any],
    ) -> Optional[str]:
        """SAL-2886: Return the SAL-NNNN identifier of the grounding-gap
        Linear issue created by the escalate-path emit, or None if not
        found.
        """
        if not isinstance(result, dict):
            return None
        for call in (result.get("tool_calls") or []):
            if not isinstance(call, dict) or call.get("name") != "linear_create_issue":
                continue
            out = call.get("result")
            if isinstance(out, str):
                try:
                    out = json.loads(out)
                except (ValueError, TypeError):
                    continue
            if isinstance(out, dict) and out.get("identifier"):
                return str(out["identifier"])
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
            f"Gate 4 (per-criterion evidence) is mandatory: for each APE/V "
            f"criterion either quote one diff line of evidence OR mark "
            f"DEFERRED-RUNTIME-VERIFICATION; reject placeholder/stub bodies "
            f"(`assert True`, `pass`, `NotImplementedError`) and any plan-doc "
            f"that admits 'Placeholder implementations may need to be replaced'.\n"
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

        0. Truthy ``intended_event`` on a ``pr_review`` tool-call result
           (or top-level of the envelope). AB-17-r: when the GitHub
           reviews API rejects a self-authored review, ``pr_review``
           returns ``state=COMMENTED_FALLBACK`` + ``intended_event``
           carrying the verdict the reviewer tried to land. Trust that
           directly so the orchestrator doesn't cycle forever
           (SAL-2663, 2026-04-24).
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

        # Priority 0: AB-17-r — honor `intended_event` regardless of `state`.
        tc0 = result.get("tool_calls") or []
        if isinstance(tc0, list):
            for call in tc0:
                if not isinstance(call, dict):
                    continue
                if (call.get("name") or "").lower() != "pr_review":
                    continue
                out = call.get("result") or call.get("output") or {}
                if isinstance(out, dict):
                    intended = out.get("intended_event")
                    if isinstance(intended, str) and intended.strip():
                        ev = intended.strip().upper()
                        if ev in ("APPROVE", "REQUEST_CHANGES"):
                            return ev
        top_intended = result.get("intended_event")
        if isinstance(top_intended, str) and top_intended.strip():
            ev = top_intended.strip().upper()
            if ev in ("APPROVE", "REQUEST_CHANGES"):
                return ev

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
                # AB-17-k priority-1b: the mesh-task daemon persists
                # tool-call *arguments*, not *results* — so `out.state`
                # is always empty. v8-smoke-e SAL-2583 (trace 115):
                # hawkman emitted pr_review(event="REQUEST_CHANGES", ...)
                # but result was bare, priority-2 regex missed the
                # past-tense "Requested changes" in the envelope, and
                # verdict returned None. Inspect arguments directly.
                args = call.get("arguments") or call.get("args") or call.get("input")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = None
                if isinstance(args, dict):
                    event = args.get("event") or args.get("state")
                    if isinstance(event, str):
                        event = event.strip().upper()
                        if event in ("APPROVE", "REQUEST_CHANGES", "COMMENT", "COMMENTED_FALLBACK"):
                            return event

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
        # AB-17-p: any verdict handled this tick (APPROVE merge, REQUEST_CHANGES
        # respawn, silent retry, etc.) is forward progress by watchdog standards.
        if updated:
            self._last_progress_ts = time.time()
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

        # SAL-2869 Layer 2 - destructive-PR verdict override.
        # If hawkman approved a PR that violates the destructive-PR
        # guardrail, OVERRIDE to REQUEST_CHANGES regardless of what
        # hawkman said. The override reason is appended to the respawn
        # body so the builder sees exactly which gate tripped and why.
        # Fail-open on infra error: a transport-level glitch fetching
        # the PR diff must not block legitimate merges.
        if verdict == "APPROVE":
            try:
                guardrail = (
                    await self._check_destructive_guardrail_for_ticket(ticket)
                )
            except Exception:
                logger.exception(
                    "destructive_guardrail: override-pass raised for %s; "
                    "letting verdict stand as APPROVE",
                    ticket.identifier,
                )
                guardrail = GuardrailResult(tripped=False)

            if guardrail.tripped:
                citations_str = (
                    "; ".join(guardrail.citations) or "(no citations)"
                )
                logger.warning(
                    "[guardrail-override] PR %s tripped: %s | %s",
                    ticket.pr_url, guardrail.reason, citations_str,
                )
                self.state.record_event(
                    "verdict_overridden_destructive",
                    identifier=ticket.identifier,
                    pr_url=ticket.pr_url,
                    layer=guardrail.layer,
                    reason=guardrail.reason,
                    citations=list(guardrail.citations),
                )
                # Override the in-memory verdict + re-record on state.
                verdict = "REQUEST_CHANGES"
                self.state.review_verdicts[ticket.id] = verdict
                # Surface the override reason to the respawn body so
                # the builder sees which gate tripped. We squirrel it
                # onto rec.result so _extract_review_body picks it up.
                if isinstance(rec, dict) and isinstance(
                    rec.get("result"), dict
                ):
                    existing_summary = rec["result"].get("summary") or ""
                    override_note = (
                        f"\n\n[SAL-2869 destructive-PR guardrail override "
                        f"({guardrail.layer})] {guardrail.reason} | "
                        f"citations: {citations_str}"
                    )
                    rec["result"]["summary"] = (
                        existing_summary + override_note
                    )

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
                # Sub #62 (2026-04-27): green merge resets the
                # consecutive-hard-timeout counter for the builder role.
                # Auto-rollback set itself only clears on registry mtime
                # change so a fluky-but-recovering primary can't
                # re-promote without operator review.
                try:
                    from .model_registry import record_success
                    record_success("builder")
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "model_registry.record_success failed; "
                        "counter not reset"
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
            #
            # SAL-3038 / SAL-3070 follow-up (2026-04-28): apply the same
            # human-assigned + terminal-state gate that PRs #259 (wave-loop
            # dispatch) and #265 (bare-claim path) installed at the two
            # primary dispatch points. The respawn path was flagged as
            # secondary risk by the post-incident audit: if a Linear ticket
            # acquires the ``human-assigned`` label or transitions to a
            # terminal state (``Done`` / ``Cancelled`` / ``Duplicate``)
            # AFTER the build PR was opened but BEFORE Hawkman files
            # REQUEST_CHANGES, the original dispatch gate has already
            # passed and only this site can stop a builder from being
            # respawned against a ticket the orchestrator should no longer
            # touch. Same shared predicate, same skip semantics: mark
            # ESCALATED, record the skip, do NOT increment review_cycles
            # or clear the review task id (we treat it as out-of-band
            # closure, not a forward step in the review loop).
            from ..main import _should_skip_for_human_or_terminal
            _gate_skip, _gate_reason = _should_skip_for_human_or_terminal({
                "labels": getattr(ticket, "labels", None) or [],
                "state": "",
            })
            if _gate_skip:
                logger.info(
                    "%s respawn-path gate fired; skipping fix-round "
                    "respawn (treated as terminal-success; reason=%s)",
                    ticket.identifier, _gate_reason,
                )
                ticket.status = TicketStatus.ESCALATED
                self.state.record_event(
                    "respawn_skipped_human_or_terminal",
                    identifier=ticket.identifier,
                    reason=_gate_reason,
                )
                updated.append(ticket)
                return
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

    # SAL-2869 destructive-PR guardrail wiring.
    #
    # Three layers, one shared helper (compute_destructive_guardrails):
    #
    # - Layer 1 (preventive): builder system prompt in persona.py
    #   carries the DELETION GUARDRAIL clause. Tested by
    #   tests/test_destructive_guardrail.py.
    #
    # - Layer 2 (verdict gate): _handle_review_verdict calls
    #   _check_destructive_guardrail_for_ticket BEFORE acting on an
    #   APPROVE verdict. If the guardrail trips, the verdict is
    #   OVERRIDDEN to REQUEST_CHANGES and the override reason is rolled
    #   into the respawn body.
    #
    # - Layer 3 (pre-merge static check): _merge_pr runs the same
    #   helper one more time as a belt-and-braces gate. If it trips at
    #   merge time (e.g. hawkman approved blind, override missed it),
    #   the merge is REFUSED, the ticket is marked FAILED, and Linear
    #   is set to Backlog with the citations attached.
    #
    # Why both Layer 2 and Layer 3? Layer 2 is the cheap, common path
    # (programmatic override before merge). Layer 3 catches every other
    # path into _merge_pr - manual operator merges, future
    # auto-merge variants, restart-resume races. Two checks, one helper,
    # zero duplication.

    def _hint_for_ticket(self, ticket: Ticket):
        """Look up the TargetHint for ticket.code.

        Returns None for tickets with no parsed code or codes not
        in the static _TARGET_HINTS table - guardrail then runs
        with hint_description="" (no deletion-license keywords
        possible) which is the safe-default.
        """
        code = (ticket.code or "").upper()
        if not code:
            return None
        return _TARGET_HINTS.get(code)

    @staticmethod
    def _ticket_has_refactor_label(ticket: Ticket) -> bool:
        """Case-insensitive `refactor` label presence check."""
        labels = getattr(ticket, "labels", None) or []
        for lbl in labels:
            if isinstance(lbl, str) and lbl.strip().lower() == "refactor":
                return True
        return False

    async def _fetch_pr_files_for_guardrail(
        self, ticket: Ticket
    ) -> Optional[List[Dict[str, Any]]]:
        """Fetch GET repos/.../pulls/{N}/files for the ticket's PR.

        Returns the raw list (each entry has filename, status,
        additions, deletions) or None on transport / parse failure.
        The guardrail caller treats None as "indeterminate - fail safe
        and DO NOT trip" - we never want to block a merge on a flaky
        GitHub API.
        """
        if not ticket.pr_url:
            return None
        m = re.search(
            r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", ticket.pr_url
        )
        if not m:
            return None
        owner, repo, num = m.group(1), m.group(2), m.group(3)
        try:
            data = await self._gh_api(
                f"repos/{owner}/{repo}/pulls/{num}/files?per_page=100"
            )
        except Exception:
            logger.exception(
                "destructive_guardrail: pr files fetch failed for %s",
                ticket.pr_url,
            )
            return None
        if not isinstance(data, list):
            return None
        return data

    async def _check_destructive_guardrail_for_ticket(
        self, ticket: Ticket
    ) -> GuardrailResult:
        """Run the SAL-2869 destructive-PR guardrail against ticket's PR.

        Best-effort only: any transport / lookup failure returns a
        non-tripped result so the caller proceeds (fail-open on infra
        flakiness, fail-closed on a confirmed destructive diff).
        """
        pr_files = await self._fetch_pr_files_for_guardrail(ticket)
        if pr_files is None:
            return GuardrailResult(tripped=False)

        hint = self._hint_for_ticket(ticket)
        hint_description = hint.notes if (hint and hint.notes) else ""
        base_ref = hint.base_branch if hint else "main"

        # Resolve owner/repo from the PR URL.
        base_repo = ""
        if ticket.pr_url:
            m = re.search(
                r"github\.com/([^/]+)/([^/]+)/pull/\d+", ticket.pr_url
            )
            if m:
                base_repo = f"{m.group(1)}/{m.group(2)}"

        return compute_destructive_guardrails(
            pr_files,
            hint_description=hint_description,
            has_refactor_label=self._ticket_has_refactor_label(ticket),
            base_repo=base_repo,
            base_ref=base_ref,
        )

    async def _post_destructive_guardrail_linear_comment(
        self, ticket: Ticket, guardrail: GuardrailResult
    ) -> None:
        """Post a Linear comment when the SAL-2869 guardrail blocks a merge.

        Best-effort. The comment carries the layer, reason, and
        citations so a human can audit the refusal without spelunking
        soul-memory. If linear_add_comment is not in BUILTIN_TOOLS
        (older deploy), we silently skip - the soul
        merge_blocked_destructive event still records the trip.
        """
        try:
            from alfred_coo.tools import BUILTIN_TOOLS
        except Exception:
            logger.debug("tools not importable; skipping guardrail comment")
            return
        spec = BUILTIN_TOOLS.get("linear_add_comment")
        if spec is None:
            return
        body_lines = [
            f"## SAL-2869 destructive-PR guardrail tripped ({guardrail.layer})",
            "",
            f"**Reason:** {guardrail.reason}",
            "",
            "**Citations:**",
        ]
        for c in guardrail.citations:
            body_lines.append(f"- {c}")
        body_lines.extend([
            "",
            f"PR `{ticket.pr_url}` was REFUSED by the orchestrator's "
            "pre-merge static check. Status: FAILED. Linear state moved "
            "to Backlog. Human intervention required.",
        ])
        body = "\n".join(body_lines)
        try:
            await spec.handler(issue_id=ticket.id, body=body)
        except Exception:
            logger.exception(
                "linear_add_comment raised for guardrail trip on %s",
                ticket.identifier,
            )

    async def _post_escalated_linear_comment(
        self, ticket: Ticket, grounding_gap_ident: Optional[str]
    ) -> None:
        """SAL-2893: Post a Linear comment explaining the SAL-2886 escalate
        transition and linking the spawned grounding-gap issue.

        Best-effort. Mirrors ``_post_destructive_guardrail_linear_comment``
        - if ``linear_add_comment`` is not in BUILTIN_TOOLS (older deploy)
        we silently skip; the soul ``ticket_escalated`` event still records
        the same ``grounding_gap`` identifier so the audit trail survives
        the missing comment. ``grounding_gap_ident`` matches whatever
        ``record_event(kind="ticket_escalated", grounding_gap=...)``
        recorded on this same tick (see SAL-2886 / persona.py:58-67).
        """
        try:
            from alfred_coo.tools import BUILTIN_TOOLS
        except Exception:
            logger.debug("tools not importable; skipping escalated comment")
            return
        spec = BUILTIN_TOOLS.get("linear_add_comment")
        if spec is None:
            return
        if grounding_gap_ident:
            link_line = (
                f"**Grounding gap:** {grounding_gap_ident} "
                f"(https://linear.app/saluca/issue/{grounding_gap_ident})"
            )
        else:
            link_line = (
                "**Grounding gap:** identifier unavailable in child envelope"
            )
        body_lines = [
            "## SAL-2886 escalate-path fired (SAL-2893, 2026-04-27 follow-up)",
            "",
            link_line,
            "",
            (
                "The alfred-coo-a builder emitted a `linear_create_issue` "
                "tool call resolving to a grounding-gap issue rather than "
                "opening a PR. The orchestrator transitioned this parent "
                "ticket to `Backlog` and applied the `human-assigned` "
                "label. Backlog escapes AB-17-y's orphan-active sweep "
                "(which only scans `In Progress`) without falsely "
                "claiming the work shipped — the original SAL-2893 fix "
                "wrote `Done` here, which on 2026-04-27 marked four "
                "tickets in MC v1 GA done with no PR merged. The "
                "`human-assigned` label gates the dispatch-gate "
                "(fix/dispatch-gate-human-assigned-AB-17-v) so next "
                "kickoff excuses this ticket until a human resolves the "
                "grounding gap. Operator: inspect the linked grounding-gap "
                "issue, resolve the gap, then remove the `human-assigned` "
                "label (or re-file the ticket) to let the daemon pick it "
                "back up."
            ),
        ]
        body = "\n".join(body_lines)
        try:
            await spec.handler(issue_id=ticket.id, body=body)
        except Exception:
            logger.exception(
                "linear_add_comment raised for escalated transition on %s",
                ticket.identifier,
            )

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

        SAL-2869 Layer 3: BEFORE merging, run the destructive-PR
        guardrail one more time. If it trips here, REFUSE the merge
        (return False); the caller will mark the ticket FAILED and
        push Linear back to Backlog so a human can intervene.
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

        # SAL-2869 Layer 3 - pre-merge destructive-PR static check.
        # Best-effort: a tripped guardrail REFUSES the merge and marks
        # the ticket failed via the caller's `merged is False` branch.
        # A non-tripped result (or any infra failure) lets the merge
        # proceed; we never block on flaky GitHub API responses.
        try:
            guardrail = await self._check_destructive_guardrail_for_ticket(
                ticket
            )
        except Exception:
            logger.exception(
                "destructive_guardrail: pre-merge check raised for %s; "
                "proceeding with merge (fail-open on infra error)",
                ticket.identifier,
            )
            guardrail = GuardrailResult(tripped=False)

        if guardrail.tripped:
            citations_str = "; ".join(guardrail.citations) or "(no citations)"
            logger.error(
                "[merge-block] PR %s tripped destructive guardrail "
                "(layer=%s): %s | citations: %s",
                ticket.pr_url,
                guardrail.layer,
                guardrail.reason,
                citations_str,
            )
            self.state.record_event(
                "merge_blocked_destructive",
                identifier=ticket.identifier,
                pr_url=ticket.pr_url,
                layer=guardrail.layer,
                reason=guardrail.reason,
                citations=list(guardrail.citations),
            )
            await self._update_linear_state(ticket, "Backlog")
            await self._post_destructive_guardrail_linear_comment(
                ticket, guardrail
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

    async def _lookup_pr_branch(self, pr_url: Optional[str]) -> Optional[str]:
        """Fetch ``head.ref`` for a GitHub PR URL via the REST API.

        AB-17-o: the respawn body needs to name the branch so the child
        can call ``update_pr`` against it. We do NOT persist branch on the
        Ticket dataclass (propose_pr returns it but our orchestrator
        previously discarded it), so look it up live. Best-effort: on
        transport / auth / 404, returns ``None`` and the caller renders
        a placeholder that fails loud rather than pushing to a wrong
        branch.
        """
        if not pr_url:
            return None
        m = _PR_URL_RE.search(pr_url)
        if not m:
            return None
        # Parse owner / repo / number out of the url for the api call.
        # _PR_URL_RE only captures the whole URL, so re-parse with a
        # dedicated pattern here.
        sub = re.search(
            r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url
        )
        if not sub:
            return None
        owner, repo, num = sub.group(1), sub.group(2), sub.group(3)
        try:
            data = await self._gh_api(f"repos/{owner}/{repo}/pulls/{num}")
        except Exception:
            logger.exception("pr branch lookup failed for %s", pr_url)
            return None
        if not data:
            return None
        head = data.get("head") or {}
        ref = head.get("ref")
        return str(ref) if ref else None

    async def _render_prior_pr_block(self, ticket: Ticket) -> str:
        """Render the ``## Prior PR`` section for a fix-round respawn.

        Body pins the existing PR URL + branch so the respawned child
        knows to call ``update_pr`` (AB-17-o) instead of ``propose_pr``.
        If the branch lookup fails, the block still emits with a
        ``(lookup failed)`` marker so the child surfaces it as a
        grounding gap rather than silently opening a new PR.
        """
        branch = await self._lookup_pr_branch(ticket.pr_url)
        branch_line = branch or "(lookup failed — escalate via linear_create_issue)"
        return (
            "## Prior PR\n"
            f"url: {ticket.pr_url}\n"
            f"branch: {branch_line}\n"
            "\n"
            "This ticket was previously submitted as the PR above and "
            "received REQUEST_CHANGES. To apply the review feedback below, "
            "use the `update_pr` tool to push a new commit to the EXISTING "
            "branch — do NOT call `propose_pr` (which would create a "
            "duplicate PR on a new branch).\n"
        )

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
        # AB-17-k (2026-04-24): respawn body now includes the same
        # ``## Target`` block rendered on initial dispatch. v8-smoke-e
        # SAL-2634 showed the fix-round child had no target grounding and
        # silent-escalated, because the original respawn body skipped the
        # block that `_child_task_body` emits. Mirror it here so the
        # respawned child knows owner/repo/paths.
        target_block = _render_target_block(
            ticket.code,
            vr=self._verified_hints.get(ticket.code),
        )

        # AB-17-o (2026-04-24): look up the existing PR's head.ref so the
        # respawned child can call the new ``update_pr`` tool against the
        # same branch. v8-full-v4 wave-0 exposed the duplicate-PR leak:
        # each respawn was calling ``propose_pr`` with a fresh timestamped
        # branch, opening a NEW PR per cycle (acs#59/60, ts#4/5, ss#17/18).
        # ``_lookup_pr_branch`` is best-effort; if it fails we still render
        # the ``## Prior PR`` section with a placeholder so the child knows
        # to skip ``propose_pr`` and surface the failure explicitly.
        prior_pr_block = await self._render_prior_pr_block(ticket)

        # SAL-2978 (2026-04-25): include the `Size:` line in the fix-round
        # body so `_peek_size_label` (in `alfred_coo/main.py`) can still
        # derive the size-gated iteration cap on respawn. Without this, the
        # fix-round body had no size signal and `_peek_size_label` fell
        # through to None → size-S default cap (12), which was the wall
        # SAL-2588 TIR-06 hit 3x in v7aa. Pair with the +4 fix-round bump
        # in `_builder_iteration_cap` so the respawn gets size-S=16 / size-M=20.
        size_line = f"Size: {ticket.size}\n" if ticket.size else ""

        # 2026-04-27: include the canonical APE/V acceptance block on
        # fix-round respawns too. SAL-2965 documented fix-round drift
        # (builders rewrite the plan doc on respawn and the auto-inject
        # ships a paraphrased citation); embedding the verbatim Linear
        # text here gives the builder a stable reference even when the
        # plan doc has drifted.
        apev_block = self._render_apev_acceptance_block(ticket)

        body = (
            f"Ticket: {ticket.identifier} ({ticket.code or 'no-code'}){cp_line}\n"
            f"Linear: https://linear.app/saluca/issue/{ticket.identifier}\n"
            f"Wave: {ticket.wave}\n"
            f"Epic: {ticket.epic}\n"
            f"{size_line}"
            f"Parent autonomous_build kickoff: {self.task_id}\n"
            f"Previous PR: {ticket.pr_url}\n"
            f"Review round: {round_num} of {MAX_REVIEW_CYCLES}\n"
            f"\n"
            f"{target_block}"
            f"\n"
            f"{prior_pr_block}"
            f"\n"
            f"{apev_block}"
            f"\n"
            f"## Fix-round checklist\n"
            f"- [ ] Address every point in the review feedback below.\n"
            f"- [ ] Tests still green (`ruff` + `pytest`).\n"
            f"- [ ] Push fixes to the EXISTING branch for {ticket.pr_url} "
            f"via the `update_pr` tool; do NOT open a new PR. The reviewer "
            f"bot will re-review automatically once your new commit lands.\n"
            f"- [ ] If you replace the PR `body`, it MUST keep the "
            f"`## APE/V Acceptance (machine-checkable)` heading + "
            f"verbatim acceptance lines (auto-inject is skipped on "
            f"`update_pr`, so YOU own the citation on every body "
            f"replacement).\n"
            f"\n"
            f"## Review feedback\n"
            f"{review_excerpt or _NO_REVIEW_BODY_NOTE}\n"
            f"\n"
            f"## Plan doc context\n"
            f"{plan_doc}\n"
            f"\n"
            f"## Instructions\n"
            f"Push fixes to the existing branch via `update_pr`; do NOT "
            f"call `propose_pr` (that would open a duplicate PR). The "
            f"reviewer bot will re-review automatically.\n"
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
        failures if ≥`self.wave_green_ratio_threshold` of the wave
        merged green.

        AB-17-w (2026-04-25): the threshold is configurable per-kickoff
        (payload field ``wave_green_ratio_threshold``, default
        ``SOFT_GREEN_THRESHOLD``). Tickets bearing the ``human-assigned``
        label, plus tickets whose pre-dispatch hint verification returned
        ``PATH_CONFLICT`` or ``NO_HINT``, are still excluded from the
        numerator (they cannot count as green) but starting 2026-04-28
        they are now COUNTED IN THE DENOMINATOR. See ``_is_wave_gate_excused``
        for the exact excusal predicate.

        SAL-3072 (2026-04-28): mining sub found 97% of wave-gate passes
        (83/86 in 7d) were force-passes where most tickets were excused
        but the gate reported ratio=1.00 (passed) because excused were
        excluded from the denominator. Fix: include excused in the
        denominator so a wave with 5 green + 9 excused reports
        ratio=5/14≈0.36 (failed) instead of 5/5=1.00 (passed). Empty waves
        and all-excused waves now fail the gate (ratio=0.0) instead of
        silently passing. Threshold unchanged.
        """
        wave_tickets = self.graph.tickets_in_wave(wave_n)
        if not wave_tickets:
            return
        while not all(t.status in TERMINAL_STATES for t in wave_tickets):
            await asyncio.sleep(self.poll_sleep_sec)
            # Drive the loop forward — in real operation this would be the
            # dispatch loop doing the work. In tests we advance statuses
            # directly between ticks.

            # AB-17-n + SAL-2870: parity with _dispatch_wave deadlock
            # detector. If nothing is in-flight and BLOCKED tickets remain,
            # we are stuck on deps whose FAILED upstreams will never
            # transition. Coerce to FAILED so the classifier below can
            # apply soft-green or halt. Scoped tightly to BLOCKED (not all
            # non-terminal) because PENDING tickets may legitimately
            # transition out-of-band — that's a different deadlock class
            # caught by _dispatch_wave before we reach here. SAL-2870 adds
            # the same grace-window semantics as _dispatch_wave so the
            # gate doesn't pre-empt a pending retry. BACKED_OFF tickets
            # are NOT in the BLOCKED set so they keep ticking through the
            # cooling window naturally.
            blocked = [
                t for t in wave_tickets if t.status == TicketStatus.BLOCKED
            ]
            in_flight_here = [
                t for t in wave_tickets
                if t.status in ACTIVE_TICKET_STATES
            ]
            cooling = [
                t for t in wave_tickets
                if t.status == TicketStatus.BACKED_OFF
            ]
            if blocked and not in_flight_here and not cooling:
                if self._no_progress_since is None:
                    self._no_progress_since = time.time()
                stuck_for = time.time() - self._no_progress_since
                if stuck_for >= self.deadlock_grace_sec:
                    blocked_ids = [t.identifier for t in blocked]
                    logger.error(
                        "wave %d gate deadlock: %d BLOCKED tickets with no "
                        "in-flight or cooling for %.0fs (grace=%ds); "
                        "coercing to FAILED: %s",
                        wave_n, len(blocked), stuck_for,
                        self.deadlock_grace_sec, blocked_ids,
                    )
                    for t in blocked:
                        t.status = TicketStatus.FAILED
                        self.state.record_event(
                            "ticket_forced_failed_gate_deadlock",
                            identifier=t.identifier,
                            stuck_for_sec=int(stuck_for),
                        )
                    self._no_progress_since = None
                    break
            elif in_flight_here or cooling:
                # Reset grace timer when forward motion is observable.
                self._no_progress_since = None

        # Wave is terminal. Classify.
        # AB-17-d · Plan I §1.4 — tickets that were skipped due to
        # REPO_MISSING are marked FAILED internally so the wave loop
        # terminates, but they represent a grounding gap (missing repo),
        # not an execution failure. Exclude them from both the `failed`
        # list (so they don't trip critical-path halt or soft-green
        # denominator) and the ratio denominator (so a wave that is 5/5
        # green + 1 repo-missing still reports 100% green).
        effective = [
            t for t in wave_tickets
            if t.id not in self._repo_missing_tickets
        ]
        # AB-17-w · Plan AB-17-w — additional excusal axes on top of the
        # AB-17-d REPO_MISSING exclusion already applied above. These
        # tickets are excluded from the green-ratio NUMERATOR (they
        # cannot count as green merges) but as of SAL-3072 (2026-04-28)
        # they ARE counted in the DENOMINATOR — work that was scoped to
        # the wave but didn't ship is still uncompleted work:
        #   1. ``human-assigned`` label — Cristian (or another human) is
        #      handling this out-of-band; the orchestrator never owned it.
        #   2. PATH_CONFLICT verification — the static target hint pointed
        #      at a path that already exists in a way the spec didn't
        #      anticipate; no actionable PR can be opened until a human
        #      resolves the conflict.
        #   3. NO_HINT (code not in ``_TARGET_HINTS``) — pre-existing
        #      grounding gap; the orchestrator has no idea where this
        #      ticket's PR should land. Equivalent to "never had a
        #      TargetHint at all".
        # Pre-SAL-3072 these were excluded from BOTH numerator and
        # denominator, which produced the force-pass bug (5 green + 9
        # excused → 5/5 = 1.00 → "passed" when really only 5/14 actually
        # shipped). Post-fix: green / (green + failed + excused).
        excused = [
            t for t in effective
            if self._is_wave_gate_excused(t)
        ]
        excused_ids = {t.id for t in excused}
        scored = [t for t in effective if t.id not in excused_ids]

        failed = [t for t in scored if t.status == TicketStatus.FAILED]
        cp_failed = [t for t in failed if t.is_critical_path]
        green = [t for t in scored if t.status == TicketStatus.MERGED_GREEN]
        threshold = float(self.wave_green_ratio_threshold)
        # SAL-3072: include excused in denominator (was len(scored)).
        # Empty waves now report ratio=0.0 and fail the threshold instead
        # of vacuously passing. All-excused waves likewise fail (correctly
        # surfacing that the wave shipped nothing).
        denominator = len(scored) + len(excused)
        green_ratio = (len(green) / denominator) if denominator > 0 else 0.0

        # AB-17-w: structured wave-end log line. Always emitted, regardless
        # of decision, so an operator tailing logs sees the full math
        # (numerator, denominator, excused count, threshold, ratio,
        # decision) on one line.
        # SAL-3072: removed "skipped_all_excused" decision — all-excused
        # waves now flow through the normal threshold check (they fail
        # at ratio=0.0).
        decision = (
            "halted_critical_path" if cp_failed
            else "passed" if green_ratio >= threshold
            else "failed_below_threshold"
        )
        logger.info(
            "[wave-gate] wave=%d green=%d failed=%d excused=%d "
            "denominator=%d threshold=%.2f ratio=%.2f decision=%s",
            wave_n, len(green), len(failed), len(excused),
            denominator, threshold, green_ratio, decision,
        )

        if cp_failed:
            msg = (
                f"wave {wave_n} has {len(cp_failed)} critical-path failure(s): "
                + ", ".join(t.identifier for t in cp_failed)
            )
            logger.error(msg)
            self.state.record_event("wave_halt_critical_path", wave=wave_n,
                                    failed=[t.identifier for t in cp_failed])
            raise RuntimeError(msg)

        # SAL-3072 (2026-04-28): the AB-17-w "all-excused = pass" branch
        # was removed. With excused now counted in the denominator, an
        # all-excused wave reports ratio=0.0 and fails the threshold like
        # any other wave that didn't ship work. The decision tree below is
        # now gated purely on `green_ratio >= threshold`, not on the
        # presence of FAILED tickets — pre-fix, an excused-only wave
        # (failed=[], green=[]) fell through to the all-green branch and
        # got logged as success. Now it raises with ratio=0.0 < threshold.

        if green_ratio < threshold:
            # Below threshold. Distinguish "real failures pulled the ratio
            # down" from "nothing shipped (all excused, or empty post-
            # repo-missing-filter)" for ops triage, but both raise.
            if failed:
                msg = (
                    f"wave {wave_n} failed: green_ratio={green_ratio:.2f} < "
                    f"{threshold:.2f} and {len(failed)} non-critical failure(s)"
                )
            elif denominator <= 0:
                # All tickets were repo-missing-filtered. Pre-SAL-3072
                # this would have been masked as a vacuous all-excused
                # pass; now surfaced as a halt so ops sees the grounding
                # gap.
                msg = (
                    f"wave {wave_n} failed: zero scoreable tickets "
                    f"(all REPO_MISSING-filtered); orchestrator state "
                    f"corrupt or grounding gap"
                )
            else:
                # Excused-dominant wave: no failures, no greens, just
                # tickets the orchestrator never owned. Pre-SAL-3072 this
                # was the force-pass scenario the mining sub flagged.
                msg = (
                    f"wave {wave_n} failed: green_ratio={green_ratio:.2f} < "
                    f"{threshold:.2f} (green={len(green)} excused="
                    f"{len(excused)} of {denominator}); nothing shipped"
                )
            logger.error(msg)
            self.state.record_event(
                "wave_halt_below_soft_green",
                wave=wave_n,
                failed=[t.identifier for t in failed],
                excused=[t.identifier for t in excused],
                excused_count=len(excused),
                green_ratio=green_ratio,
                threshold=threshold,
            )
            raise RuntimeError(msg)

        # green_ratio >= threshold. Either a true all-green wave (no
        # failures) or a soft-green pass (some failures, but enough
        # greens to clear the bar).
        if failed:
            logger.warning(
                "wave %d soft-green: %d/%d merged (%d excused), "
                "non-critical failures: %s",
                wave_n, len(green), denominator, len(excused),
                [t.identifier for t in failed],
            )
            self.state.record_event(
                "wave_soft_green",
                wave=wave_n,
                failed=[t.identifier for t in failed],
                excused=[t.identifier for t in excused],
                excused_count=len(excused),
                green_ratio=green_ratio,
                threshold=threshold,
            )
            return

        logger.info("wave %d all-green", wave_n)
        self.state.record_event(
            "wave_all_green",
            wave=wave_n,
            excused_count=len(excused),
        )
        # Fix A: persist the pass record so a subsequent kickoff/restart
        # can short-circuit this wave via ``_should_skip_wave``. Only
        # written on the *true* all-green branch (no failures, ratio above
        # threshold). The wave-skip cache key is the (project, wave_n)
        # pair, so a soft-green pass should NOT poison the cache for a
        # later run.
        #
        # 2026-04-28: writer must persist the FULL wave membership
        # (`wave_tickets`), not the post-excusal `scored` set. The
        # reader (`_should_skip_wave`) compares against
        # `self.graph.tickets_in_wave(wave_n)` and treats any cached/live
        # diff as "new tickets added → invalidate". If the writer omits
        # excused tickets (human-assigned, PATH_CONFLICT, NO_HINT), the
        # reader sees them as "new" on every subsequent kickoff and the
        # cache structurally never fires for any wave that contains an
        # excused ticket. See RCA: wave-skip-cache-rca-2026-04-28.md.
        try:
            await record_wave_pass(
                self.soul,
                linear_project_id=self.linear_project_id,
                wave_n=wave_n,
                ratio=green_ratio,
                denominator=denominator,
                green_count=len(green),
                ticket_codes_seen=[t.identifier for t in wave_tickets],
            )
        except Exception:
            logger.exception(
                "record_wave_pass failed for wave %d; cache will miss next "
                "run but pass itself stands", wave_n,
            )

    def _is_wave_gate_excused(self, ticket: "Ticket") -> bool:
        """AB-17-w: True iff `ticket` should be excluded from the wave-gate
        green-ratio denominator. See ``_wait_for_wave_gate`` for the three
        excusal axes (human-assigned label, PATH_CONFLICT verification,
        NO_HINT / unmapped code).

        Centralised so the same predicate is used in any future caller
        (e.g. status-tick rendering) and so tests can exercise the
        decision in isolation.
        """
        # Axis 1: human-assigned label (case-insensitive name match).
        labels = getattr(ticket, "labels", None) or []
        if any(
            isinstance(lbl, str) and lbl.lower() == HUMAN_ASSIGNED_LABEL
            for lbl in labels
        ):
            return True

        # Axes 2 + 3: per-code hint verification at wave start. The
        # `_verified_hints` cache is keyed by the uppercase ticket code.
        # Verification only runs in real `run()` flow, so the cache is
        # empty when an operator (or test) drives `_wait_for_wave_gate`
        # directly. We deliberately do NOT fall back to a direct
        # `_TARGET_HINTS` lookup when the cache is empty — that would
        # excuse every ticket whose code isn't pre-mapped, including
        # tickets the orchestrator legitimately tried to build. The cache
        # is the only signal that says "verification ran AND told us this
        # ticket was never actionable".
        code_key = (ticket.code or "").upper()
        if code_key:
            vr = self._verified_hints.get(code_key)
            # SAL-3281: NEW_PATHS_COLLISION mirrors PATH_CONFLICT for the
            # wave-gate denominator — scaffold-only collisions are not the
            # ticket's fault; the renderer treats them as benign and the
            # builder still ships a PR. Keeping them excused preserves the
            # green-ratio semantics (these tickets are bonus shipping, not
            # required work for the wave threshold).
            if vr is not None and vr.status in (
                HintStatus.PATH_CONFLICT,
                HintStatus.NEW_PATHS_COLLISION,
                HintStatus.NO_HINT,
            ):
                return True

        # SAL-2886: per-ticket terminal-escalated mirrors the wave-level
        # PATH_CONFLICT excusal so a ticket whose escalate path fired
        # post-dispatch (i.e. was not caught by wave-start verification)
        # is also excluded from the green ratio.
        if ticket.status == TicketStatus.ESCALATED:
            return True

        return False

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

        # Fix B: piggy-back the stale "In Progress" sweep on the cadence
        # tick (every ``status_cadence_min`` minutes by default). Cheap
        # when nothing is stale (one Linear list + zero GitHub calls);
        # bounded to the active project so a single missed cadence tick
        # is at most a few API hits.
        try:
            await self._sweep_stale_in_progress(self.linear_project_id)
        except Exception:
            logger.exception(
                "_sweep_stale_in_progress raised during cadence tick; continuing"
            )

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

        AB-06 implementation, extended by SAL-2890 (Fix D + Fix E).
        Contract:
          - Non-SS-08 tickets: no-op, return True.
          - `self.state.ss08_acked` already True: skip gate, return True.
          - Soul-memory has a fresh ACK record for
            ``(linear_project_id, "SS-08")``: short-circuit the poll, flip
            the in-process flag, return True. Survives daemon restarts.
          - Otherwise run `run_ss08_gate(cadence, slack_ack_poll_fn,
            on_ack_detected=...)`:
              * On ACK: persist to soul memory + set `state.ss08_acked = True`,
                checkpoint, return True (dispatch proceeds).
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

        # Fix D: pre-ACK check from soul memory. Survives daemon restarts.
        # Without this, every daemon bounce burns a fresh "ACK SS-08" round-
        # trip with Cristian — observed 3x across v7ab/v7ac/v7ad on 2026-04-26.
        from .state import lookup_gate_ack, is_gate_ack_fresh, record_gate_ack

        gate_name = "SS-08"
        if self.linear_project_id:
            try:
                prior = await lookup_gate_ack(
                    self.soul,
                    linear_project_id=self.linear_project_id,
                    gate_name=gate_name,
                )
            except Exception:
                logger.exception(
                    "lookup_gate_ack raised; falling through to live poll"
                )
                prior = None

            if prior is not None and is_gate_ack_fresh(prior):
                logger.info(
                    "[gate-ack] %s pre-acked at %s by %s; skipping poll",
                    gate_name, prior.acked_at,
                    prior.acked_by_user_id or "(unknown)",
                )
                self.state.ss08_acked = True
                self.state.record_event(
                    "ss08_gate_preacked",
                    identifier=ticket.identifier,
                    acked_at=prior.acked_at,
                    acked_by_user_id=prior.acked_by_user_id,
                    ack_message_ts=prior.ack_message_ts,
                )
                await checkpoint(self.state, self.soul, self.task_id)
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

        # Fix D persistence callback. Captured by reference so we can hand
        # it to ``run_ss08_gate``; closes over self for project + soul access.
        async def _persist_ack(payload: Dict[str, Any]) -> None:
            if not self.linear_project_id:
                logger.debug(
                    "skipping gate-ack persist: linear_project_id unset"
                )
                return
            try:
                await record_gate_ack(
                    self.soul,
                    linear_project_id=self.linear_project_id,
                    gate_name=gate_name,
                    acked_by_user_id=payload.get("acked_by_user_id"),
                    ack_message_ts=payload.get("ack_message_ts"),
                    ack_message_text=payload.get("ack_message_text"),
                    acked_at=payload.get("acked_at"),
                )
            except Exception:
                logger.exception(
                    "record_gate_ack raised; in-process flip still applies"
                )

        try:
            acked = await run_ss08_gate(
                cadence=self.cadence,
                slack_ack_poll_fn=poll_fn,
                logger_=logger,
                on_ack_detected=_persist_ack,
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

    async def _apply_linear_label(self, ticket: Ticket, label_name: str) -> None:
        """Attach a Linear label to a ticket via AB-03's ``linear_add_label_to_issue``.

        Mirrors ``_update_linear_state`` exactly: import + spec lookup +
        defensive try/except. Failure is logged and swallowed — our graph
        stays source of truth, and the worst case is the dispatch-gate
        skip-on-``human-assigned`` doesn't fire on the next kickoff
        (orphan-active still won't trip because Linear was just moved
        out of "In Progress").

        Used by the ESCALATED branch in ``_poll_children`` to mark
        grounding-gap tickets as ``human-assigned`` so the dispatch-gate
        fix (fix/dispatch-gate-human-assigned-AB-17-v) excuses them on
        subsequent kickoffs until a human resolves the gap. Idempotent
        server-side per Linear's API contract.
        """
        try:
            from alfred_coo.tools import BUILTIN_TOOLS
        except Exception:
            logger.debug("tools not importable; skipping Linear label apply")
            return
        spec = BUILTIN_TOOLS.get("linear_add_label_to_issue")
        if spec is None:
            return
        try:
            resp = await spec.handler(issue_id=ticket.id, label_name=label_name)
            if isinstance(resp, dict) and resp.get("error"):
                logger.warning(
                    "linear_add_label_to_issue(%s, %s) returned error: %s",
                    ticket.identifier, label_name, resp["error"],
                )
        except Exception:
            logger.exception(
                "linear_add_label_to_issue raised for %s -> %s",
                ticket.identifier, label_name,
            )

    # ── Wave-iteration optimizations (Fix A + Fix B) ───────────────────────
    #
    # Fix A · Wave-skip cache. When a wave previously passed at ratio=1.00
    # within the freshness window (default 24h), short-circuit re-entry
    # rather than burning a full hint-verify + dispatch + gate cycle.
    # The cache invalidates if:
    #   - prior pass was below 1.00 (soft-greens are never cached),
    #   - the freshness window has elapsed,
    #   - any ticket in the wave has moved BACKWARD since the pass
    #     (e.g. Done -> Backlog), OR
    #   - the cached ticket-code set disagrees with the current graph
    #     (tickets added/removed between runs).
    # Fix B · Stale "In Progress" sweeper. Cleans up tickets whose PR
    # merged but Linear state never flipped (observed: SAL-2656 OPS-23 in
    # v7ab/v7ac/v7ad — PR merged but state stuck, eating wave-1 deadlock-
    # grace cycles on every kickoff). Sweep runs at startup and on each
    # cadence tick, bounded to the active project.

    # Default freshness for the wave-skip cache. Overridable per-test via
    # ``self._wave_pass_freshness_sec`` (set in __init__ below). 24h
    # matches the daily kickoff cadence — anything older should be re-
    # evaluated to catch overnight drift in Linear/GitHub state.
    _WAVE_PASS_FRESHNESS_SEC: int = 24 * 60 * 60

    # GitHub PR-search lookback for the stale-sweep helper. 7 days is
    # generous: a ticket that's been "In Progress" for >7 days with no
    # merged PR is more likely a genuine in-flight build (or a Linear
    # workflow oddity) than a missed state-flip. Override via
    # ``self._stale_sweep_pr_lookback_sec``.
    _STALE_SWEEP_PR_LOOKBACK_SEC: int = 7 * 24 * 60 * 60

    # Bound on how many candidate "In Progress" tickets the sweep examines
    # per call. Prevents an oversized project from blowing the cadence
    # tick. 50 covers every observed wave + a comfortable buffer.
    _STALE_SWEEP_MAX_CANDIDATES: int = 50

    async def _should_skip_wave(self, wave_n: int) -> bool:
        """Return True iff this wave previously passed at ratio=1.00 in a
        recent run AND the wave's ticket set has not regressed since.

        Best-effort. Soul-svc miss / parse error / project-id missing all
        return False (the safe fallback — re-evaluate as normal). The
        caller logs the skip + records a ``wave_skipped_pre_passed`` event.
        """
        if not self.linear_project_id:
            return False
        record = await lookup_wave_pass(
            self.soul,
            linear_project_id=self.linear_project_id,
            wave_n=wave_n,
        )
        if record is None:
            return False
        # Soft-greens are never cached, but defend anyway.
        if record.ratio < 1.0:
            return False
        freshness = getattr(
            self, "_wave_pass_freshness_sec", self._WAVE_PASS_FRESHNESS_SEC,
        )
        if not is_wave_pass_fresh(record, max_age_sec=int(freshness)):
            logger.debug(
                "[wave-skip] wave=%d cache stale (passed_at=%s); re-evaluating",
                wave_n, record.passed_at,
            )
            return False
        # Reconcile against the live graph: every cached ticket must still
        # exist AND every wave-N ticket must currently be terminal-green
        # in our local graph (i.e. MERGED_GREEN). If any ticket regressed
        # to a non-green state since the cache was written — Done -> Backlog
        # is the prime example — invalidate.
        wave_tickets = self.graph.tickets_in_wave(wave_n)
        live_idents = {t.identifier for t in wave_tickets}
        cached_idents = set(record.ticket_codes_seen or [])
        # New tickets added since the pass → re-evaluate so the new ones
        # get dispatched. Removed tickets are tolerable (ticket may have
        # been cancelled/duplicated; cached pass still applies to the
        # rest), but added tickets MUST flush the cache.
        added = live_idents - cached_idents
        if added:
            logger.info(
                "[wave-skip] wave=%d cache invalidated: %d new ticket(s) "
                "added since pass (%s); re-evaluating",
                wave_n, len(added), sorted(added)[:5],
            )
            return False
        # Backward transitions: any ticket NOT currently MERGED_GREEN
        # invalidates the cache. The orchestrator's restore path already
        # mirrors Linear state into local TicketStatus via
        # ``_apply_restored_status``, so a Done -> Backlog flip in Linear
        # surfaces here as ``status != MERGED_GREEN``.
        regressed = [
            t for t in wave_tickets
            if t.status != TicketStatus.MERGED_GREEN
        ]
        if regressed:
            logger.info(
                "[wave-skip] wave=%d cache invalidated: %d ticket(s) "
                "regressed since pass (%s); re-evaluating",
                wave_n, len(regressed),
                [t.identifier for t in regressed[:5]],
            )
            return False
        return True

    async def _sweep_stale_in_progress(self, project_id: str) -> int:
        """Find Linear tickets in ``project_id`` stuck in "In Progress" with
        a merged PR, and flip them to Done.

        Returns the number of tickets flipped (mostly for tests + logging).
        Best-effort: any failure inside the sweep is logged and swallowed
        — never raises into the caller. Idempotent: a ticket already in
        Done is silently skipped on subsequent calls.

        Heuristic for "merged PR exists":
          - GitHub Search API ``q=org:salucallc is:pr is:merged
            in:title,body <ticket_identifier>`` (e.g. ``SAL-2656``)
          - Filter to PRs merged within the last
            ``_STALE_SWEEP_PR_LOOKBACK_SEC`` (7d default).
          - Any single hit is enough — the orchestrator's local state
            tracks individual PRs separately; here we only need a signal
            that *some* recent merged PR cited the ticket code.
        """
        if not project_id:
            return 0
        try:
            from alfred_coo.tools import BUILTIN_TOOLS
        except Exception:
            logger.debug(
                "tools not importable; skipping stale-sweep for %s", project_id,
            )
            return 0
        list_spec = BUILTIN_TOOLS.get("linear_list_project_issues")
        if list_spec is None:
            logger.debug(
                "linear_list_project_issues missing; skipping stale-sweep",
            )
            return 0
        try:
            resp = await list_spec.handler(project_id=project_id)
        except Exception:
            logger.exception(
                "linear_list_project_issues raised during stale-sweep "
                "(project=%s)", project_id,
            )
            return 0
        if not isinstance(resp, dict) or resp.get("error"):
            logger.debug(
                "stale-sweep skipped: linear_list_project_issues returned %r",
                resp if not isinstance(resp, dict) else resp.get("error"),
            )
            return 0
        issues = resp.get("issues") or []
        candidates = [
            iss for iss in issues
            if (((iss.get("state") or {}).get("name") or "").strip().lower()
                == "in progress")
        ]
        if not candidates:
            logger.debug(
                "[stale-sweep] project=%s no In Progress tickets", project_id,
            )
            return 0
        if len(candidates) > self._STALE_SWEEP_MAX_CANDIDATES:
            logger.warning(
                "[stale-sweep] project=%s has %d In Progress tickets; "
                "sweeping the first %d only",
                project_id, len(candidates), self._STALE_SWEEP_MAX_CANDIDATES,
            )
            candidates = candidates[: self._STALE_SWEEP_MAX_CANDIDATES]

        update_spec = BUILTIN_TOOLS.get("linear_update_issue_state")
        comment_spec = BUILTIN_TOOLS.get("linear_add_comment")
        flipped = 0
        for iss in candidates:
            ident = iss.get("identifier") or ""
            iss_id = iss.get("id") or ""
            if not ident or not iss_id:
                continue
            try:
                merged_pr = await self._find_recent_merged_pr_for(ident)
            except Exception:
                logger.exception(
                    "[stale-sweep] PR search raised for %s; leaving as is",
                    ident,
                )
                continue
            if not merged_pr:
                logger.debug(
                    "[stale-sweep] %s in In Progress, no recent merged PR; "
                    "leaving alone (genuine in-flight)", ident,
                )
                continue
            logger.info(
                "[stale-sweep] auto-flipping %s In Progress -> Done "
                "(merged PR: %s)", ident, merged_pr,
            )
            self.state.record_event(
                "ticket_auto_flipped_stale",
                identifier=ident,
                pr_url=merged_pr,
            )
            if update_spec is not None:
                try:
                    upd = await update_spec.handler(
                        issue_id=iss_id, state_name="Done",
                    )
                    if isinstance(upd, dict) and upd.get("error"):
                        logger.warning(
                            "[stale-sweep] linear_update_issue_state(%s, "
                            "Done) returned error: %s",
                            ident, upd["error"],
                        )
                        continue
                except Exception:
                    logger.exception(
                        "[stale-sweep] linear_update_issue_state raised "
                        "for %s; skipping comment + counter",
                        ident,
                    )
                    continue
            flipped += 1
            # Best-effort audit comment. Mirrors the pattern in
            # ``_post_destructive_guardrail_linear_comment`` /
            # ``_post_escalated_linear_comment`` — silently skip if
            # ``linear_add_comment`` is not installed in this deploy.
            if comment_spec is not None:
                body = (
                    f"auto-flipped by orchestrator: stale In Progress + "
                    f"merged PR found ({merged_pr}). The Linear state was "
                    "out of sync with the merge-commit signal; flipping "
                    "to Done so wave gates stop re-dispatching this "
                    "ticket. (See "
                    "`feat/wave-skip-and-stale-state-sweeper` PR notes.)"
                )
                try:
                    await comment_spec.handler(issue_id=iss_id, body=body)
                except Exception:
                    logger.exception(
                        "[stale-sweep] linear_add_comment raised for %s; "
                        "flip already applied", ident,
                    )
        if flipped:
            logger.info(
                "[stale-sweep] project=%s flipped %d stale In Progress "
                "ticket(s) to Done", project_id, flipped,
            )
        return flipped

    # ── stale-sweep PR-to-ticket matcher (tightening rule) ─────────────────
    #
    # The 2026-04-26 verification audit caught the sweeper auto-flipping
    # tickets to Done based purely on a GitHub-Search code hit, with no
    # check that the merged PR actually edited the ticket's expected
    # files. Three confirmed misfires (TIR-15, OPS-08, F19) plus 3 likely
    # (OPS-14, FLEET-29, SS-07) all traced back to PRs #73/#109/#120/#126
    # that only edited ``src/alfred_coo/autonomous_build/orchestrator.py``
    # (adding ``_TARGET_HINTS`` strings). The sweeper read the ticket code
    # in the PR body and concluded the ticket shipped, even though no
    # implementation file changed.
    #
    # Tightening rule: a search-hit PR is treated as evidence iff the PR's
    # ``files_changed`` intersects the ticket's ``_TARGET_HINTS[code].paths
    # ∪ new_paths``. PRs whose only changed file is a known "non-evidence"
    # file (the orchestrator hints table is the canonical example — any
    # ticket can be referenced there without being implemented there) are
    # rejected outright as a fast-path defence.

    # Files that, when changed in isolation, NEVER count as evidence that
    # a ticket shipped. The orchestrator's _TARGET_HINTS dict cites every
    # ticket code by definition — a PR that only modifies this file is a
    # hints-data update, not a ticket implementation.
    _NON_EVIDENCE_FILES: Tuple[str, ...] = (
        "src/alfred_coo/autonomous_build/orchestrator.py",
    )

    @staticmethod
    def _pr_files_match_hint(
        files_changed: Tuple[str, ...],
        hint: Optional[TargetHint],
    ) -> bool:
        """Pure helper: does any file in ``files_changed`` match the hint's
        expected scope (``hint.paths ∪ hint.new_paths``)?

        Returns False when ``hint is None`` (no hint to compare against —
        cannot verify, must NOT auto-flip), when ``files_changed`` is
        empty, or when the only changed file is in the non-evidence
        allowlist (orchestrator hints table). Match is exact-string on
        repo-relative POSIX paths — the GitHub Pulls API returns paths
        in this form natively.
        """
        if hint is None:
            return False
        if not files_changed:
            return False
        # Fast-path: if the ENTIRE diff is non-evidence files, reject.
        # (A PR that touches orchestrator.py *and* a real implementation
        # file is fine — the implementation file will hit the intersect
        # below. We only short-circuit the pure-hints-table case.)
        non_evidence = set(
            AutonomousBuildOrchestrator._NON_EVIDENCE_FILES
        )
        if all(f in non_evidence for f in files_changed):
            return False
        expected = set(hint.paths) | set(hint.new_paths)
        if not expected:
            return False
        return any(f in expected for f in files_changed)

    async def _fetch_pr_files(
        self, pr_url: str,
    ) -> Optional[Tuple[str, ...]]:
        """Return the list of repo-relative file paths changed by the PR
        at ``pr_url`` (e.g. ``https://github.com/<owner>/<repo>/pull/123``),
        or ``None`` on any error / unparseable URL.

        Tests stub this via ``self._gh_pr_files_fn`` to avoid live HTTP.
        Mirrors the ``_gh_pr_search_fn`` pattern used by the sibling
        ``_find_recent_merged_pr_for`` helper.
        """
        # Test hook: bypass HTTP entirely.
        stub = getattr(self, "_gh_pr_files_fn", None)
        if stub is not None:
            try:
                files = await stub(pr_url)
            except Exception:
                logger.exception(
                    "[stale-sweep] _gh_pr_files_fn stub raised for %s",
                    pr_url,
                )
                return None
            if files is None:
                return None
            return tuple(files)
        # Parse owner/repo/num out of the html_url.
        m = re.match(
            r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)",
            pr_url or "",
        )
        if not m:
            logger.debug(
                "[stale-sweep] cannot parse PR url %r; skipping file check",
                pr_url,
            )
            return None
        owner, repo, num = m.group(1), m.group(2), m.group(3)
        token = os.environ.get("GITHUB_TOKEN") or ""
        if not token:
            logger.debug(
                "[stale-sweep] GITHUB_TOKEN unset; cannot fetch files for %s",
                pr_url,
            )
            return None
        # Pulls API: ``GET /repos/{owner}/{repo}/pulls/{num}/files``.
        # Up to 100 files per page; stale-sweep PRs are tiny, one page
        # is plenty.
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/pulls/{num}/files",
                    params={"per_page": "100"},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "saluca-alfred/1.0",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
                if r.status_code != 200:
                    logger.warning(
                        "[stale-sweep] GitHub pulls/files returned %d for "
                        "%s (body=%r)",
                        r.status_code, pr_url, r.text[:300],
                    )
                    return None
                payload = r.json()
        except Exception:
            logger.exception(
                "[stale-sweep] GitHub pulls/files raised for %s", pr_url,
            )
            return None
        if not isinstance(payload, list):
            return None
        files = tuple(
            (item.get("filename") or "")
            for item in payload
            if isinstance(item, dict) and item.get("filename")
        )
        return files

    async def _find_recent_merged_pr_for(self, ticket_ident: str) -> Optional[str]:
        """Search ``salucallc/*`` for a merged PR citing ``ticket_ident``
        in title or body within the last ``_STALE_SWEEP_PR_LOOKBACK_SEC``,
        AND whose changed files intersect the ticket's expected scope.

        Returns the first matching PR URL (or None). Uses the GitHub
        Search API directly — no gh CLI dependency, matches the rest of
        ``alfred_coo.tools`` (systemd ``ProtectHome=true``).

        Tightening rule (2026-04-26, fix for TIR-15/OPS-08/F19 misfires):
        a candidate PR is only returned if its ``files_changed`` overlaps
        ``_TARGET_HINTS[code].paths ∪ new_paths`` for the ticket. PRs that
        only touch the ``_TARGET_HINTS`` orchestrator file (or any other
        ``_NON_EVIDENCE_FILES`` entry) are rejected even when the search
        hit them by code mention. If the ticket has no hint we cannot
        verify scope, so we conservatively return None — the sweeper
        leaves it for human review rather than auto-flip on weak evidence.

        Tests stub this via ``self._gh_pr_search_fn`` to avoid live HTTP.
        ``self._gh_pr_files_fn`` separately stubs the file fetch used by
        the new intersection check.
        """
        if not ticket_ident:
            return None

        # Resolve the ticket's plan-doc code so we can look up the hint.
        # ``self.graph`` is populated by the wave loop before any sweep
        # runs; if it's not yet built (early-startup race), skip rather
        # than guess.
        graph = getattr(self, "graph", None)
        ticket = graph.get_by_identifier(ticket_ident) if graph else None
        code = (getattr(ticket, "code", "") or "").upper().strip()
        hint = _TARGET_HINTS.get(code) if code else None

        candidate_urls = await self._search_recent_merged_pr_urls(
            ticket_ident,
        )
        if not candidate_urls:
            return None

        # Walk candidates in search-rank order, returning the first that
        # passes the file-overlap check.
        for url in candidate_urls:
            if hint is None:
                # No hint to compare against. Refuse to auto-flip — the
                # whole point of the tightening rule is to require
                # positive evidence, and a code-search hit on a PR with
                # no scope contract isn't enough.
                logger.info(
                    "[stale-sweep] candidate PR %s found for %s but no "
                    "_TARGET_HINTS entry exists for code %r; refusing to "
                    "auto-flip on weak evidence",
                    url, ticket_ident, code,
                )
                continue
            files_changed = await self._fetch_pr_files(url)
            if files_changed is None:
                logger.debug(
                    "[stale-sweep] file fetch failed for %s (ticket %s); "
                    "skipping this candidate",
                    url, ticket_ident,
                )
                continue
            if not self._pr_files_match_hint(files_changed, hint):
                logger.info(
                    "[stale-sweep] candidate PR %s for %s does NOT "
                    "intersect expected scope (paths=%r new_paths=%r "
                    "changed=%r); rejecting as not-an-implementation",
                    url, ticket_ident, hint.paths, hint.new_paths,
                    files_changed[:8],
                )
                continue
            return url
        return None

    async def _search_recent_merged_pr_urls(
        self, ticket_ident: str,
    ) -> List[str]:
        """Return the list of merged-PR ``html_url`` values from a
        GitHub-Search hit on ``ticket_ident`` within the lookback window.

        Extracted from ``_find_recent_merged_pr_for`` so the search step
        is unit-testable independently of the file-overlap intersection
        step. Honours the ``self._gh_pr_search_fn`` stub for legacy tests
        (which return a single URL instead of a list).
        """
        # Test hook: bypass HTTP entirely. Legacy stubs return a single
        # url-or-None (matching the original signature); wrap so the new
        # caller sees a list either way.
        stub = getattr(self, "_gh_pr_search_fn", None)
        if stub is not None:
            try:
                result = await stub(ticket_ident)
            except Exception:
                logger.exception(
                    "[stale-sweep] _gh_pr_search_fn stub raised for %s",
                    ticket_ident,
                )
                return []
            if result is None:
                return []
            if isinstance(result, str):
                return [result]
            # Allow newer stubs to return a list directly.
            return [u for u in result if isinstance(u, str)]
        # Live path — guarded by env presence; missing token returns []
        # so the sweep degrades to a no-op rather than erroring out.
        token = os.environ.get("GITHUB_TOKEN") or ""
        if not token:
            logger.debug(
                "[stale-sweep] GITHUB_TOKEN unset; skipping PR search for %s",
                ticket_ident,
            )
            return []
        # GitHub Search API. Ref:
        #   https://docs.github.com/en/rest/search/search#search-issues-and-pull-requests
        # We look for PRs in the salucallc org, merged, that mention the
        # ticket identifier anywhere in title or body. Date filter narrows
        # the lookback so an old PR doesn't trigger a stale-flip.
        cutoff_struct = time.gmtime(
            time.time() - self._STALE_SWEEP_PR_LOOKBACK_SEC
        )
        cutoff = time.strftime("%Y-%m-%d", cutoff_struct)
        q = (
            f"org:salucallc is:pr is:merged in:title,body "
            f"{ticket_ident} merged:>={cutoff}"
        )
        params = {"q": q, "per_page": "5"}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(
                    "https://api.github.com/search/issues",
                    params=params,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "saluca-alfred/1.0",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
                if r.status_code != 200:
                    logger.warning(
                        "[stale-sweep] GitHub search returned %d for %s "
                        "(body=%r)",
                        r.status_code, ticket_ident, r.text[:300],
                    )
                    return []
                payload = r.json()
        except Exception:
            logger.exception(
                "[stale-sweep] GitHub search raised for %s", ticket_ident,
            )
            return []
        items = (payload or {}).get("items") or []
        urls: List[str] = []
        for item in items:
            url = item.get("html_url") or ""
            if "/pull/" in url:
                urls.append(url)
        return urls

    # ── SAL-3038 PR-exists short-circuit (2026-04-28) ───────────────────────

    async def _ticket_has_open_pr_awaiting_review(
        self, ticket_ident: str,
    ) -> Optional[int]:
        """Return the PR number if an open PR for ``ticket_ident`` exists
        on ``salucallc/*`` and is awaiting Hawkman review; else None.

        Background: SAL-3038 (PR #214) was re-dispatched 68 times in 7
        days because the dispatch loop didn't check whether an open PR
        already existed for the ticket. Each cycle burned $0.50-2 in
        tokens producing nothing useful. This helper closes the gap by
        querying the GitHub Search API for an open PR mentioning the
        ticket identifier in title or body and inspecting its review
        state.

        Returns the PR number when ALL of:
          - At least one open PR matches the ticket identifier search.
          - The PR was created within ``PR_EXISTS_FRESH_PR_WINDOW_SEC``
            (default 7 days). Older PRs are treated as stale; dispatch
            proceeds to open a fresh attempt.
          - The PR has NO APPROVED review from ``HAWKMAN_LOGIN``. Once
            Hawkman approves, dispatch should proceed (the merge bot is
            the next step, not another builder).

        Returns None on any of:
          - No matching open PR found.
          - PR is too old (>7 days).
          - Hawkman has already APPROVED.
          - GitHub API failure (degrades to "dispatch as before"; the
            existing dispatch idempotency + retry-budget paths still
            cap the blast radius).

        Results are cached per-ticket for ``PR_EXISTS_CACHE_TTL_SEC`` so
        a 16-ticket wave doesn't fire 16+ Search calls every 45s tick.
        Tests stub the network round-trip via
        ``self._gh_pr_open_search_fn`` (signature documented at the
        ledger field's __init__ docstring).
        """
        if not ticket_ident:
            return None

        # Test-environment guard: when the orchestrator is exercised under
        # pytest WITHOUT an explicit ``_gh_pr_open_search_fn`` stub, the
        # caller hasn't opted into the network round-trip. Returning None
        # immediately keeps the live API out of the hot path of dispatch-
        # loop tests that don't care about this short-circuit. Tests that
        # DO care set the stub, which dominates the check below in
        # ``_fetch_open_pr_awaiting_review``.
        if (
            self._gh_pr_open_search_fn is None
            and os.environ.get("PYTEST_CURRENT_TEST")
        ):
            return None

        now = time.time()
        cached = self._pr_exists_cache.get(ticket_ident)
        if cached is not None:
            cached_at, cached_pr = cached
            if now - cached_at < PR_EXISTS_CACHE_TTL_SEC:
                return cached_pr

        pr_number: Optional[int] = None
        try:
            pr_number = await self._fetch_open_pr_awaiting_review(
                ticket_ident, now=now,
            )
        except Exception:
            logger.exception(
                "[pr-exists] check raised for %s; treating as no-PR "
                "and proceeding with dispatch",
                ticket_ident,
            )
            pr_number = None

        self._pr_exists_cache[ticket_ident] = (now, pr_number)
        return pr_number

    async def _fetch_open_pr_awaiting_review(
        self, ticket_ident: str, *, now: float,
    ) -> Optional[int]:
        """Inner half of ``_ticket_has_open_pr_awaiting_review``: do the
        actual GitHub round-trip (or stub call) and apply the freshness +
        APPROVED-review filters. Split out so the cache layer above stays
        legible.
        """
        stub = self._gh_pr_open_search_fn
        if stub is not None:
            try:
                payload = await stub(ticket_ident)
            except Exception:
                logger.exception(
                    "[pr-exists] _gh_pr_open_search_fn stub raised for %s",
                    ticket_ident,
                )
                return None
            return self._evaluate_pr_payload(payload, now=now)

        token = os.environ.get("GITHUB_TOKEN") or ""
        if not token:
            logger.debug(
                "[pr-exists] GITHUB_TOKEN unset; skipping PR-exists "
                "check for %s (dispatch will proceed)",
                ticket_ident,
            )
            return None

        # GitHub Search: open PRs in the salucallc org mentioning the
        # ticket identifier in title or body. ``per_page=3`` is enough —
        # we only need the most-recent match.
        q = (
            f"org:salucallc is:pr is:open in:title,body "
            f"{ticket_ident}"
        )
        params = {
            "q": q,
            "per_page": "3",
            "sort": "created",
            "order": "desc",
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "saluca-alfred/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(
                    "https://api.github.com/search/issues",
                    params=params,
                    headers=headers,
                )
                if r.status_code != 200:
                    logger.warning(
                        "[pr-exists] GitHub search returned %d for %s "
                        "(body=%r)",
                        r.status_code, ticket_ident, r.text[:300],
                    )
                    return None
                items = (r.json() or {}).get("items") or []
                if not items:
                    return None
                # Walk in created-desc order, returning the first PR that
                # is fresh + unreviewed by Hawkman.
                for item in items:
                    pr_url = item.get("pull_request", {}).get("url") or ""
                    if not pr_url:
                        continue
                    rr = await client.get(
                        f"{pr_url}/reviews",
                        headers=headers,
                    )
                    reviews: List[Dict[str, Any]] = []
                    if rr.status_code == 200:
                        reviews = rr.json() or []
                    payload = {
                        "number": item.get("number"),
                        "created_at": item.get("created_at"),
                        "reviews": reviews,
                    }
                    decision = self._evaluate_pr_payload(payload, now=now)
                    if decision is not None:
                        return decision
        except Exception:
            logger.exception(
                "[pr-exists] GitHub search raised for %s", ticket_ident,
            )
            return None
        return None

    @staticmethod
    def _evaluate_pr_payload(
        payload: Optional[Dict[str, Any]], *, now: float,
    ) -> Optional[int]:
        """Apply the freshness + Hawkman-APPROVED filter to a single PR
        payload (search-item + reviews). Returns the PR number when the
        short-circuit should fire, or None to fall through.

        Pulled out as a staticmethod so the live network branch and the
        stub-driven test branch share the exact same decision logic.
        """
        if not payload:
            return None
        number = payload.get("number")
        if not isinstance(number, int):
            return None
        created_raw = payload.get("created_at")
        if isinstance(created_raw, str) and created_raw:
            try:
                # GitHub returns ISO-8601 with Z. Python <3.11 fromisoformat
                # rejects Z; replace before parsing.
                created_iso = created_raw.replace("Z", "+00:00")
                from datetime import datetime
                created_ts = datetime.fromisoformat(created_iso).timestamp()
                if now - created_ts > PR_EXISTS_FRESH_PR_WINDOW_SEC:
                    return None
            except (ValueError, TypeError):
                # Unparseable date — treat as stale rather than skip dispatch.
                return None
        reviews = payload.get("reviews") or []
        for rev in reviews:
            if not isinstance(rev, dict):
                continue
            user = (rev.get("user") or {}).get("login") or ""
            state = (rev.get("state") or "").upper()
            if user == HAWKMAN_LOGIN and state == "APPROVED":
                # Hawkman approved → merge bot is the next step, not
                # another builder. Proceed with dispatch (None) so the
                # merge path runs.
                return None
        return number

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

    # ── AB-17-q · external cancel signal (SAL-2756) ─────────────────────────

    async def _check_cancel_signal(self) -> bool:
        """Poll the kickoff task's lifecycle state for an external cancel
        signal. Returns ``True`` iff a cancel was just observed (so the
        caller can log / record an event once); idempotent — subsequent
        calls return ``False`` once ``_cancel_requested`` is already set.

        Three signal shapes accepted (any one fires the cancel):

        1. ``status == "canceled"`` — forward-compat for a future soul-svc
           lifecycle state. The current v2.0.0 enum is ``completed|failed``
           only, but `_check_cancel_signal` matches case-insensitively so
           the contract holds the moment soul-svc adds it.
        2. ``status == "failed"`` AND ``result.cancel == True`` — the
           SAL-2756 design-sketch path. The operator marks the kickoff
           failed and stamps ``cancel: true`` in the result blob to
           distinguish a deliberate cancel from a crash-completion.
        3. ``status == "failed"`` with no ``cancel`` flag — treated as a
           cancel iff the orchestrator is still ticking (we wouldn't be
           in this method otherwise). Operator workflow: ``mesh.complete
           --status failed --reason external_cancel`` is the documented
           way to stop a runaway wave; we honour the signal regardless of
           whether the result blob carries the explicit flag.

        Best-effort: any HTTP / JSON failure returns ``False`` and lets
        the next tick retry. Cancel never raises into the dispatch loop.
        """
        if self._cancel_requested:
            return False
        try:
            rec = await self.mesh.get_task(self.task_id)
        except Exception:
            logger.exception(
                "cancel-signal poll failed for kickoff %s; will retry next tick",
                self.task_id,
            )
            return False
        if not isinstance(rec, dict):
            return False

        status = (rec.get("status") or "").lower()
        result = rec.get("result") or {}
        cancel_flag = bool(result.get("cancel")) if isinstance(result, dict) else False

        if status == "canceled" or cancel_flag or status == "failed":
            reason = ""
            if isinstance(result, dict):
                reason = str(
                    result.get("reason")
                    or result.get("error")
                    or ""
                )[:500]
            if not reason:
                reason = f"external_cancel:status={status or 'unknown'}"

            # SAL-2890: defend against self-inflicted cancels.
            #
            # The daemon's main task-claim loop can spuriously re-claim its
            # own already-running orchestrator parent task during long
            # stalls (heartbeat-claim staleness in soul-svc). The duplicate-
            # kickoff guard in main.py rejects the second claim by setting
            # mesh status=failed with reason "duplicate_kickoff: existing
            # orchestrator task=<own_id> running for project=<id>". If we
            # honor that as a cancel, we kill our own recovery. The reason
            # field is structurally diagnosable: prefix + own task id.
            #
            # Ignore the signal in that case, logging a WARNING so the race
            # remains visible. External operator cancels (different reason
            # OR same reason naming a different task id) are still honored.
            if reason.startswith("duplicate_kickoff:") and self.task_id in reason:
                logger.warning(
                    "[cancel] ignoring self-inflicted duplicate-kickoff cancel "
                    "signal for kickoff %s (reason=%s); main-loop spuriously "
                    "re-claimed own running task. SAL-2890.",
                    self.task_id, reason,
                )
                # Do NOT set _cancel_requested; do NOT enter drain mode.
                # Caller treats False return as "no cancel observed."
                return False

            self._cancel_requested = True
            self._cancel_reason = reason
            self._drain_mode = True
            logger.warning(
                "[cancel] external cancel signal observed for kickoff %s "
                "(status=%s cancel_flag=%s reason=%s); entering drain mode",
                self.task_id, status, cancel_flag, reason,
            )
            self.state.record_event(
                "cancel_requested",
                task_id=self.task_id,
                status=status,
                cancel_flag=cancel_flag,
                reason=reason,
            )
            return True
        return False

    async def _complete_kickoff_canceled(self) -> None:
        """AB-17-q: terminal handler for the graceful-cancel path.

        Snapshots state, records a ``kickoff_canceled`` event, and posts a
        final ``status="failed"`` complete with ``result.cancel = True``
        so the kickoff record clearly reflects an operator-driven stop
        rather than a crash. The operator-side PATCH that triggered the
        cancel will already have flipped the DB record — this call is
        idempotent (soul-svc returns 409 / 200 on re-complete) and serves
        primarily to attach the orchestrator-side state snapshot for
        post-mortem.
        """
        self._snapshot_graph_into_state()
        self.state.record_event(
            "kickoff_canceled",
            task_id=self.task_id,
            reason=self._cancel_reason,
            current_wave=self.state.current_wave,
        )
        try:
            await self.mesh.complete(
                self.task_id,
                session_id=self.settings.soul_session_id,
                status="failed",
                result={
                    "error": f"external_cancel: {self._cancel_reason}",
                    "cancel": True,
                    "cancel_reason": self._cancel_reason,
                    "final_state_snapshot": {
                        "current_wave": self.state.current_wave,
                        "cumulative_spend_usd": self.state.cumulative_spend_usd,
                        "ticket_status": self.state.ticket_status,
                        "events_tail": self.state.events[-10:],
                    },
                },
            )
        except Exception:
            # The operator's PATCH may have already moved the record to a
            # terminal state — soul-svc rejects re-completion with 409.
            # Log and move on; the cancel intent is already in the DB.
            logger.warning(
                "post-cancel complete failed for kickoff %s "
                "(likely already terminal); continuing",
                self.task_id,
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

    # ── AB-17-b · hint verification (Plan I §1) ────────────────────────────

    async def _gh_api(self, path: str) -> Optional[dict]:
        """``GET https://api.github.com/{path}`` → JSON on 200, ``None`` on 404.

        Raises ``httpx.HTTPStatusError`` for any other non-2xx response so
        callers can distinguish a clean-miss 404 from a transient 5xx /
        429 / timeout and decide whether to retry or mark UNVERIFIED.

        Retries once on 5xx / connection errors with a 2s sleep. On 429,
        returns by raising so the caller can decide (``_gh_contents`` maps
        it to ``"unknown"`` without retry — we don't want to hammer an
        already-rate-limited endpoint).
        """
        url = f"https://api.github.com/{path.lstrip('/')}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        # SAL-2905: orchestrator identity (hint-verification probes
        # are an orchestrator-tier action). Falls back through
        # ``token_for_persona``'s chain → legacy GITHUB_TOKEN.
        # SAL-2930: QA-hop removed; QA PATs scoped only to Pull
        # requests 404 on private-repo content reads.
        from alfred_coo.persona_github import (
            GitHubIdentityClass,
            token_for_persona,
            PERSONA_IDENTITY_MAP,
        )
        PERSONA_IDENTITY_MAP.setdefault(
            "_intended_orchestrator", GitHubIdentityClass.ORCHESTRATOR
        )
        token, _id_class = token_for_persona("_intended_orchestrator")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        last_exc: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(url, headers=headers)
            except (httpx.NetworkError, httpx.TimeoutException) as e:
                last_exc = e
                if attempt == 1:
                    await asyncio.sleep(2.0)
                    continue
                raise
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                # Don't retry rate-limit hits; propagate so caller marks
                # the path "unknown" and we move on.
                resp.raise_for_status()
            if 500 <= resp.status_code < 600 and attempt == 1:
                await asyncio.sleep(2.0)
                continue
            resp.raise_for_status()
        # Unreachable: either returned or raised above.
        if last_exc is not None:
            raise last_exc  # noqa: RSE102 — belt-and-braces
        raise RuntimeError("unreachable")

    async def _gh_contents(
        self, owner: str, repo: str, path: str, ref: str
    ) -> Literal["exist", "absent", "unknown"]:
        """Probe a repo path at a ref. Returns ``"exist"`` on 200,
        ``"absent"`` on 404, ``"unknown"`` on any transient failure
        (5xx after retry, 429, timeout). Never raises — Plan I §1.2
        wants a best-effort per-path outcome, not a wave-wide crash.
        """
        # URL-encode ref so `feature/foo` branches survive the query param
        # round-trip. The path itself is part of the URL so we keep it raw.
        api_path = f"repos/{owner}/{repo}/contents/{path.lstrip('/')}"
        url = f"https://api.github.com/{api_path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        # SAL-2905: orchestrator identity (hint-verification probes).
        from alfred_coo.persona_github import (
            GitHubIdentityClass,
            token_for_persona,
            PERSONA_IDENTITY_MAP,
        )
        PERSONA_IDENTITY_MAP.setdefault(
            "_intended_orchestrator", GitHubIdentityClass.ORCHESTRATOR
        )
        token, _id_class = token_for_persona("_intended_orchestrator")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        for attempt in (1, 2):
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(
                        url, headers=headers, params={"ref": ref}
                    )
            except (httpx.NetworkError, httpx.TimeoutException):
                if attempt == 1:
                    await asyncio.sleep(2.0)
                    continue
                return "unknown"
            if resp.status_code == 200:
                return "exist"
            if resp.status_code == 404:
                return "absent"
            if resp.status_code == 429:
                return "unknown"
            if 500 <= resp.status_code < 600 and attempt == 1:
                await asyncio.sleep(2.0)
                continue
            return "unknown"
        return "unknown"

    async def _verify_hint(
        self, code: str, hint: TargetHint
    ) -> VerificationResult:
        """Verify a single ``TargetHint`` against live GitHub state.

        Plan I §1.2 decision flow:

        1. ``GET repos/{owner}/{repo}`` — if 404, short-circuit to
           ``REPO_MISSING``. Other errors → ``UNVERIFIED`` so we dispatch
           anyway (transient issues shouldn't freeze a wave).
        2. For each path in ``hint.paths`` (expected to exist) and
           ``hint.new_paths`` (expected to be absent), probe contents and
           build a ``PathResult``.
        3. Aggregate: all-ok → ``OK``; any conflict in ``new_paths`` →
           ``PATH_CONFLICT``; any missing in ``paths`` → ``PATH_MISSING``;
           else ``UNVERIFIED`` (transient).
        """
        async with self._verify_semaphore:
            started_at = time.time()
            # 1. Repo existence check.
            try:
                repo_data = await self._gh_api(f"repos/{hint.owner}/{hint.repo}")
            except Exception as e:  # noqa: BLE001 — network classification
                return VerificationResult(
                    code=code,
                    hint=hint,
                    status=HintStatus.UNVERIFIED,
                    repo_exists=False,
                    path_results=(),
                    error=f"repo probe failed: {type(e).__name__}: {str(e)[:200]}",
                    verified_at=started_at,
                )
            if repo_data is None:
                return VerificationResult(
                    code=code,
                    hint=hint,
                    status=HintStatus.REPO_MISSING,
                    repo_exists=False,
                    path_results=(),
                    error=(
                        f"repo {hint.owner}/{hint.repo} returned 404 at "
                        f"ref {hint.base_branch}"
                    ),
                    verified_at=started_at,
                )

            # 2. Path checks.
            path_results: List[PathResult] = []
            any_missing_in_paths = False
            any_conflict_in_new_paths = False
            any_unknown = False

            for path in hint.paths:
                observed = await self._gh_contents(
                    hint.owner, hint.repo, path, hint.base_branch
                )
                if observed == "exist":
                    path_results.append(PathResult(
                        path=path, expected="exist", observed="exist", ok=True,
                    ))
                elif observed == "absent":
                    path_results.append(PathResult(
                        path=path, expected="exist", observed="absent", ok=False,
                    ))
                    any_missing_in_paths = True
                else:
                    path_results.append(PathResult(
                        path=path, expected="exist", observed="unknown", ok=False,
                    ))
                    any_unknown = True

            for path in hint.new_paths:
                observed = await self._gh_contents(
                    hint.owner, hint.repo, path, hint.base_branch
                )
                if observed == "exist":
                    path_results.append(PathResult(
                        path=path, expected="absent", observed="exist", ok=False,
                    ))
                    any_conflict_in_new_paths = True
                elif observed == "absent":
                    path_results.append(PathResult(
                        path=path, expected="absent", observed="absent", ok=True,
                    ))
                else:
                    path_results.append(PathResult(
                        path=path, expected="absent", observed="unknown", ok=False,
                    ))
                    any_unknown = True

            # 3. Status aggregation.
            #
            # SAL-3281 (2026-04-28): split NEW_PATHS_COLLISION out of
            # PATH_CONFLICT. The collision arm fires when the *only* problem
            # is that one or more ``new_paths`` entries already exist AND at
            # least one ``new_paths`` entry is still absent (i.e. the ticket
            # has real CREATE work left — the typical case is a plan-doc
            # scaffold pre-authored by the hint-batching sub). Mixed
            # collisions + path-missing keep the legacy PATH_CONFLICT
            # tiebreak. Full-collision (every new_paths exists, paths all
            # OK) also stays PATH_CONFLICT — that's the genuine "work was
            # already merged in an earlier wave" case where the persona
            # SHOULD escalate via grounding-gap.
            absent_new_paths_count = sum(
                1 for pr in path_results
                if pr.expected == "absent" and pr.observed == "absent"
            )
            new_paths_collision_only = (
                any_conflict_in_new_paths
                and not any_missing_in_paths
                and not any_unknown
                and absent_new_paths_count > 0
            )
            if all(pr.ok for pr in path_results):
                status = HintStatus.OK
                error: Optional[str] = None
            elif new_paths_collision_only:
                status = HintStatus.NEW_PATHS_COLLISION
                error = (
                    "one or more new_paths already exist on main "
                    "(scaffold pre-authored); "
                    f"{absent_new_paths_count} new_paths still absent — "
                    "ticket remains actionable"
                )
            elif any_conflict_in_new_paths:
                status = HintStatus.PATH_CONFLICT
                error = "one or more new_paths already exist"
            elif any_missing_in_paths:
                status = HintStatus.PATH_MISSING
                error = "one or more paths missing"
            else:
                status = HintStatus.UNVERIFIED
                error = "one or more paths returned unknown status"

            # Short-circuit: if truly all unknown (no missing, no conflict,
            # but paths also aren't all ok), surface that explicitly.
            if not any_missing_in_paths and not any_conflict_in_new_paths \
                    and any_unknown and status != HintStatus.OK:
                status = HintStatus.UNVERIFIED

            return VerificationResult(
                code=code,
                hint=hint,
                status=status,
                repo_exists=True,
                path_results=tuple(path_results),
                error=error,
                verified_at=started_at,
            )

    async def _verify_wave_hints(
        self, wave: int
    ) -> Dict[str, VerificationResult]:
        """Verify every ticket in the wave. Keyed by ticket code.

        Tickets with no code or whose code is not in ``_TARGET_HINTS``
        return a ``NO_HINT`` result so the renderer can emit the
        ``(unresolved)`` block deterministically. Fan-out is bounded by
        ``self._verify_semaphore``; the outer gather just waits for every
        per-ticket coroutine to settle.
        """
        tickets = self.graph.tickets_in_wave(wave)
        results: Dict[str, VerificationResult] = {}

        async def verify_one(ticket) -> None:
            code = (ticket.code or "").upper()
            if not code:
                # Unparseable/missing code — nothing to key on. Skip so
                # downstream rendering falls through to (unresolved).
                return
            hint = _TARGET_HINTS.get(code)
            if hint is None:
                results[code] = VerificationResult(
                    code=code,
                    hint=None,
                    status=HintStatus.NO_HINT,
                    repo_exists=False,
                    path_results=(),
                    error=f"no hint for code {code}",
                    verified_at=time.time(),
                )
                return
            results[code] = await self._verify_hint(code, hint)

        await asyncio.gather(*[verify_one(t) for t in tickets])
        return results

    # ── AB-17-d · BLOCKED on REPO_MISSING ────────────────────────────────────

    async def _mark_repo_missing_tickets(
        self, wave_tickets: List[Ticket]
    ) -> None:
        """Filter `wave_tickets` against `self._verified_hints` and handle
        any whose `VerificationResult.status == REPO_MISSING` per Plan I
        §1.4 + §2.3:

        1. Emit a grounding-gap Linear issue (idempotent via
           ``self._emitted_blocks``).
        2. Mark the ticket ``FAILED`` internally (so the wave loop can
           reach a terminal state) without mutating Linear state — the
           parent ticket stays ``Backlog`` per Plan I §5.1 R-d / §7.
        3. Record the ticket UUID in ``self._repo_missing_tickets`` so
           ``_wait_for_wave_gate`` excludes it from the soft-green
           numerator/denominator (these tickets were never dispatched
           and must not count toward pass/fail).

        No-op when `_verified_hints` is empty (e.g. verification crashed
        upstream and was caught in `_run_inner`) — we dispatch as today
        in that degraded mode, because "skip everything" is worse than
        "let the child's Step-0 protocol catch the bad hint".
        """
        if not self._verified_hints:
            return
        for ticket in wave_tickets:
            code = (ticket.code or "").upper()
            vr = self._verified_hints.get(code)
            if vr is None or vr.status != HintStatus.REPO_MISSING:
                continue
            # Best-effort grounding-gap issue. Failure is logged but
            # non-fatal — re-emission on the next wave boundary is
            # acceptable per Plan I §5.1 R-d (idempotent enough for MVP).
            await self._emit_grounding_gap_repo_missing(ticket, vr)
            # Mark internally so the wave loop terminates. Do NOT call
            # `_update_linear_state` — ticket stays Backlog per §5.1 R-d.
            ticket.status = TicketStatus.FAILED
            self._repo_missing_tickets.add(ticket.id)
            self.state.record_event(
                "ticket_blocked_repo_missing",
                identifier=ticket.identifier,
                code=code,
                repo=f"{vr.hint.owner}/{vr.hint.repo}" if vr.hint else "unknown",
            )

    async def _emit_grounding_gap_repo_missing(
        self, ticket: Ticket, vr: VerificationResult
    ) -> None:
        """Emit a `[grounding-gap]` Linear issue for a ticket whose hint
        verification returned REPO_MISSING. Dedupes within the current
        orchestrator process via ``self._emitted_blocks``. Also emits a
        structured WARN log line so the mesh operator can spot the block
        without opening Linear. Failures (network, API, missing key) are
        logged at ERROR but never raised — the wave loop continues and
        the next wave boundary will re-verify + re-emit.
        """
        hint = vr.hint
        owner = hint.owner if hint else "unknown"
        repo = hint.repo if hint else "unknown"
        base_branch = hint.base_branch if hint else "main"
        repo_slug = f"{owner}/{repo}"

        # Structured WARN — mirrors the JSON-ish key=value shape used by
        # other orchestrator logs (e.g. `_dispatch_child`'s "dispatching
        # %s %s (wave %d, ...)" line).
        logger.warning(
            "blocked ticket=%s reason=repo_missing repo=%s code=%s",
            ticket.identifier, repo_slug, ticket.code or "(unparseable)",
        )

        # Optional dedupe: don't re-emit if we already reported this
        # ticket during the current orchestrator process. Reset happens
        # on restart — acceptable per Plan I §5.1 R-d.
        if ticket.identifier in self._emitted_blocks:
            logger.debug(
                "grounding-gap already emitted for %s; skipping duplicate",
                ticket.identifier,
            )
            return

        try:
            from alfred_coo.tools import BUILTIN_TOOLS
        except Exception:
            logger.exception(
                "alfred_coo.tools not importable; cannot emit grounding-gap "
                "for %s", ticket.identifier,
            )
            return
        spec = BUILTIN_TOOLS.get("linear_create_issue")
        if spec is None:
            logger.error(
                "linear_create_issue not in BUILTIN_TOOLS; cannot emit "
                "grounding-gap for %s", ticket.identifier,
            )
            return

        title = (
            f"[grounding-gap] BLOCKED · {ticket.identifier} · repo missing"
        )
        body = (
            "## BLOCKED by orchestrator (AB-17-d)\n"
            "\n"
            f"Ticket **{ticket.identifier}** could not be dispatched because its\n"
            f"`_TARGET_HINTS` entry points at repo `{repo_slug}` which does\n"
            "not exist (GitHub 404 on repo existence probe at wave start).\n"
            "\n"
            f"- Plan-doc code: `{ticket.code or '(unparseable)'}`\n"
            f"- Base branch hinted: `{base_branch}`\n"
            f"- Verifier diagnostic: `{vr.error or '(none)'}`\n"
            f"- Verified at: `{vr.verified_at}` (epoch seconds)\n"
            f"- Parent ticket: https://linear.app/saluca/issue/{ticket.identifier}\n"
            "\n"
            "**Resolution paths** (for the human / next session):\n"
            "1. Repo typo in `_TARGET_HINTS` → fix the hint and restart orchestrator\n"
            "2. Repo legitimately missing → create repo (e.g. via `gh repo create`), then restart\n"
            "3. Plan-doc targets wrong repo → fix plan doc + hint + restart\n"
            "\n"
            "The parent ticket stays Backlog and will be re-verified on the next wave kickoff.\n"
        )

        try:
            # `labels` arg is accepted by the handler signature but the
            # current GraphQL mutation does not yet forward it; passing
            # here is forward-compat for when it does.
            resp = await spec.handler(
                title=title,
                description=body,
                priority=2,
                labels=["grounding-gap"],
            )
            if isinstance(resp, dict) and resp.get("error"):
                logger.error(
                    "linear_create_issue returned error for grounding-gap "
                    "%s: %s", ticket.identifier, resp["error"],
                )
                return
            self._emitted_blocks.add(ticket.identifier)
        except Exception:
            logger.exception(
                "linear_create_issue raised while emitting grounding-gap "
                "for %s; wave continues, will re-emit on next wave boundary",
                ticket.identifier,
            )
