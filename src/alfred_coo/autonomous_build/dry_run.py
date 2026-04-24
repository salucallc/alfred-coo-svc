"""Dry-run adapter + orchestrator hooks for autonomous_build (AB-07).

Activated by the env var `AUTONOMOUS_BUILD_DRY_RUN=1`. Swaps every
side-effecting client used by `AutonomousBuildOrchestrator` (mesh,
slack_post, slack_ack_poll, linear_update_issue_state) for in-process
stubs so the full wave loop can be exercised without hitting Linear /
Slack / soul-svc.

Design:
  - `DryRunAdapter` stores synthesized tasks in memory + auto-completes
    them after a configurable delay so the orchestrator's
    `_poll_children` path sees realistic "completed" records with fake
    `tokens`/`model` fields (so `BudgetTracker.record` actually ticks
    the cumulative spend).
  - `DryRunMesh` is a thin shim over the adapter that matches the
    `MeshClient` surface the orchestrator uses (`create_task`,
    `list_tasks`, `complete`).
  - `apply_dry_run(orch, adapter)` mutates an orchestrator instance:
      * swaps `orch.mesh`
      * overrides `orch.cadence._slack_post_fn`
      * patches `orch._resolve_slack_ack_poll`
      * replaces `orch._update_linear_state` with a log-only no-op
  - `maybe_apply_dry_run(orch)` is the entry point: reads the env var
    and applies the adapter if set. No-op otherwise.

The smoke test at `tests/smoke/test_autonomous_build_smoke.py` drives a
3-ticket graph through this path end-to-end in <10s.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional


logger = logging.getLogger("alfred_coo.autonomous_build.dry_run")


#: Default child-task result used when no per-task override is scripted.
#:
#: The orchestrator's live `_poll_children` path (post-2026-04-23 hotfix
#: in commit e163daf, "no-PR=FAILED") now marks any completed child with a
#: missing PR URL as FAILED — the rationale being that a real child that
#: silently completed without calling `propose_pr` is buggy, not successful.
#: That hotfix broke the dry-run happy path: the original
#: `DEFAULT_DRY_RUN_RESULT` had `pr_url: None`, which made every dry-run
#: child get marked FAILED on the very next `_poll_children` tick.
#:
#: Fix: the default dry-run result now carries a fake PR URL (matches
#: `_PR_URL_RE`), a verdict-ready summary so the review polling path sees
#: APPROVE, and the same tokens/model so `BudgetTracker` still ticks.
#: `apply_dry_run` additionally patches `orch._merge_pr` so the GitHub
#: merge call is also a no-op in dry-run.
#: Fake PR URL used by the dry-run default envelope. Must match the
#: orchestrator's `_PR_URL_RE` (`https://github\.com/[^\s)]+/pull/\d+`).
_DRY_RUN_FAKE_PR_URL = "https://github.com/salucallc/alfred-coo-svc/pull/9999"


#: Default child-task result envelope used when no per-task override is
#: scripted. The orchestrator's `_extract_pr_url` reads from `summary`,
#: `content`, `tool_calls[].result.pr_url`, and `follow_up_tasks`, NOT
#: from a top-level `pr_url` field, so the fake URL is embedded inside
#: `summary`. The `APPROVE` keyword in `summary` also drives
#: `_extract_verdict` -> APPROVE on the review path, which combined with
#: the `_merge_pr` swap in `apply_dry_run` lets the full dispatch → PR
#: → review → merge flow reach MERGED_GREEN.
#:
#: This changed in 2026-04-23's "no-PR=FAILED" hotfix (commit e163daf):
#: before, the orchestrator treated a completed child with no PR URL as
#: MERGED_GREEN; after, it marks FAILED. The original dry-run default
#: (`pr_url: None`, summary with no URL) was silently broken by that
#: hotfix, leaving the smoke test red for every PR thereafter.
DEFAULT_DRY_RUN_RESULT: Dict[str, Any] = {
    "summary": (
        f"DRY-RUN auto-complete APPROVE — opened PR at "
        f"{_DRY_RUN_FAKE_PR_URL} and ready to merge."
    ),
    "pr_url": _DRY_RUN_FAKE_PR_URL,
    "tokens": {"in": 100, "out": 50},
    "model": "qwen3-coder:480b-cloud",
}


ENV_FLAG = "AUTONOMOUS_BUILD_DRY_RUN"


def dry_run_enabled() -> bool:
    """Return True when the dry-run env flag is set to a truthy value.

    Accepts "1", "true", "yes" (case-insensitive) - anything else is false.
    """
    val = os.environ.get(ENV_FLAG, "")
    return val.strip().lower() in ("1", "true", "yes", "on")


class DryRunAdapter:
    """In-memory stand-in for the mesh + slack surfaces the orchestrator
    depends on. Holds a task registry, auto-completes tasks after a
    configurable delay, and honours per-task scripted overrides.
    """

    def __init__(
        self,
        auto_complete_after_seconds: float = 1.0,
        default_result: Optional[Dict[str, Any]] = None,
    ) -> None:
        if auto_complete_after_seconds < 0:
            raise ValueError(
                f"auto_complete_after_seconds must be >= 0 "
                f"(got {auto_complete_after_seconds!r})"
            )
        self.auto_complete_after_seconds: float = float(auto_complete_after_seconds)
        self.default_result: Dict[str, Any] = dict(
            default_result or DEFAULT_DRY_RUN_RESULT
        )
        self._counter: int = 0
        # task_id -> {record, created_at, result_override?}
        self._tasks: Dict[str, Dict[str, Any]] = {}
        # Direct-post log + ack-poll log - asserted by the smoke test.
        self.slack_posts: List[Dict[str, Any]] = []
        self.ack_polls: List[Dict[str, Any]] = []
        self.linear_updates: List[Dict[str, Any]] = []
        self.completions: List[Dict[str, Any]] = []

    # -- task surface ------------------------------------------------

    async def create_task(
        self,
        title: str,
        description: str = "",
        from_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._counter += 1
        task_id = f"dryrun-{self._counter}"
        rec = {
            "id": task_id,
            "title": title,
            "description": description,
            "from_session_id": from_session_id,
            "status": "pending",
        }
        self._tasks[task_id] = {
            "record": rec,
            "created_at": time.time(),
            "result_override": None,
        }
        logger.info("[DRY-RUN mesh] create_task id=%s title=%s", task_id, title)
        return dict(rec)

    async def list_tasks(
        self,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return the current view of synthesized tasks.

        Tasks whose created_at is at least `auto_complete_after_seconds`
        in the past are materialised as "completed" with the configured
        result (or a per-task override). Tasks younger than the delay
        stay "pending".
        """
        now = time.time()
        out: List[Dict[str, Any]] = []
        for entry in self._tasks.values():
            rec = dict(entry["record"])
            age = now - entry["created_at"]
            if age >= self.auto_complete_after_seconds:
                rec["status"] = "completed"
                rec["result"] = dict(
                    entry["result_override"] or self.default_result
                )
            else:
                rec["status"] = "pending"
            out.append(rec)
        if status:
            out = [
                r for r in out
                if (r.get("status") or "").lower() == status.lower()
            ]
        if limit and limit > 0:
            out = out[: int(limit)]
        return out

    async def complete(
        self,
        task_id: str,
        session_id: str,
        result: Dict[str, Any],
        status: str = "completed",
    ) -> Dict[str, Any]:
        """Record a completion call. Used by the orchestrator to mark the
        kickoff task done at the end of `run()`.
        """
        entry = self._tasks.get(task_id)
        rec = {
            "task_id": task_id,
            "session_id": session_id,
            "status": status,
            "result": result,
        }
        self.completions.append(rec)
        if entry is not None:
            entry["record"]["status"] = status
            entry["record"]["result"] = result
        logger.info(
            "[DRY-RUN mesh] complete id=%s status=%s", task_id, status,
        )
        return {"id": task_id, "status": status}

    # -- scripting ---------------------------------------------------

    def set_scripted_result(
        self,
        task_id: str,
        result_dict: Dict[str, Any],
    ) -> None:
        """Override the auto-complete result for a specific synthesized
        task. Subsequent `list_tasks` calls will return this result once
        the task's completion delay elapses.
        """
        if task_id not in self._tasks:
            # Pre-seed a scripted slot so the orchestrator's next dispatch
            # of this id finds the override (mostly useful for tests that
            # want to tag a ticket with a FAILED outcome up-front).
            self._tasks[task_id] = {
                "record": {
                    "id": task_id, "title": "(scripted)",
                    "description": "", "from_session_id": None,
                    "status": "pending",
                },
                "created_at": time.time(),
                "result_override": dict(result_dict),
            }
            return
        self._tasks[task_id]["result_override"] = dict(result_dict)

    def script_next(self, result_dict: Dict[str, Any]) -> None:
        """Override the result of the MOST RECENTLY created task.

        Useful for smoke tests that need to tag the last-dispatched child
        with a specific tokens/model so the budget tracker sees a specific
        spend.
        """
        if not self._tasks:
            raise RuntimeError("no tasks synthesized yet; cannot script_next")
        latest_id = f"dryrun-{self._counter}"
        self.set_scripted_result(latest_id, result_dict)

    # -- slack surface -----------------------------------------------

    async def slack_post(
        self,
        message: str = "",
        channel: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Stand-in for the `slack_post` tool handler. Prints to stdout
        and returns a fake ts/channel so the cadence layer keeps its
        contract.
        """
        # Positional-only callers are handled by the wrapper below; still
        # tolerate them here for direct use.
        if not message and "text" in kwargs:
            message = kwargs["text"]
        self.slack_posts.append(
            {"message": message, "channel": channel, "ts": time.time()}
        )
        print(f"[DRY-RUN slack] {channel or '(default)'}: {message}")
        return {
            "ts": f"{time.time():.6f}",
            "channel": channel or "C0DRYRUN",
        }

    async def slack_ack_poll(
        self,
        channel: str = "",
        after_ts: str = "",
        author_user_id: str = "",
        keywords: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Auto-ACK stand-in - returns an immediate match so the SS-08
        gate passes in dry-run. Logs the call for test assertions.
        """
        kw = list(keywords or []) or ["ACK SS-08"]
        matched_keyword = kw[0] if kw else "ACK SS-08"
        self.ack_polls.append(
            {
                "channel": channel,
                "after_ts": after_ts,
                "author_user_id": author_user_id,
                "keywords": kw,
            }
        )
        logger.info(
            "[DRY-RUN slack_ack_poll] auto-ACK for keywords=%s", kw,
        )
        return {
            "matched": True,
            "message_ts": "1",
            "matched_keyword": matched_keyword,
            "text": f"AUTO-ACK {matched_keyword}",
        }

    # -- linear surface ----------------------------------------------

    async def linear_update_issue_state(
        self,
        issue_id: str = "",
        state_name: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Stand-in for the Linear state-update tool. Logs only."""
        rec = {"issue_id": issue_id, "state_name": state_name}
        self.linear_updates.append(rec)
        logger.info(
            "[DRY-RUN linear] update_issue_state issue=%s state=%s",
            issue_id, state_name,
        )
        return {"ok": True, "identifier": issue_id, "state": state_name}


class DryRunMesh:
    """Shim matching the subset of `MeshClient` the orchestrator uses.

    Wraps a `DryRunAdapter` so tests + operators can assert state on the
    adapter without reaching through a two-level handle.
    """

    def __init__(self, adapter: DryRunAdapter) -> None:
        self.adapter = adapter

    async def create_task(
        self,
        *,
        title: str,
        description: str = "",
        from_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self.adapter.create_task(
            title=title,
            description=description,
            from_session_id=from_session_id,
        )

    async def list_tasks(
        self,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        return await self.adapter.list_tasks(status=status, limit=limit)

    async def complete(
        self,
        task_id: str,
        *,
        session_id: str,
        result: Dict[str, Any],
        status: str = "completed",
    ) -> Dict[str, Any]:
        return await self.adapter.complete(
            task_id=task_id,
            session_id=session_id,
            result=result,
            status=status,
        )


# -- orchestrator wiring ---------------------------------------------------


def apply_dry_run(orch: Any, adapter: Optional[DryRunAdapter] = None) -> DryRunAdapter:
    """Swap an orchestrator's side-effecting clients for the dry-run
    adapter. Returns the adapter so callers can inspect logs / script
    per-task overrides.

    Idempotent - calling twice re-binds cleanly.
    """
    if adapter is None:
        adapter = DryRunAdapter()

    # 1. Mesh swap.
    orch.mesh = DryRunMesh(adapter)

    # 2. Slack cadence - override the post function in place if cadence
    # exists, else stash the adapter on the orchestrator so later
    # `_parse_payload` rebuilds respect it.
    cadence = getattr(orch, "cadence", None)
    if cadence is not None:
        cadence._slack_post_fn = adapter.slack_post

    # 3. SS-08 ack-poll resolver.
    def _dry_resolve_ack_poll() -> Any:
        return adapter.slack_ack_poll

    orch._resolve_slack_ack_poll = _dry_resolve_ack_poll  # type: ignore[assignment]

    # 4. Linear update bookkeeping - swap the bound method with a
    # coroutine that routes through the adapter log.
    async def _dry_update_linear_state(ticket: Any, state_name: str) -> None:
        try:
            identifier = getattr(ticket, "id", "") or ""
        except Exception:
            identifier = ""
        await adapter.linear_update_issue_state(
            issue_id=identifier,
            state_name=state_name,
        )

    orch._update_linear_state = _dry_update_linear_state  # type: ignore[assignment]

    # 5. _merge_pr swap. The dry-run default child result now carries a
    # fake PR URL so _poll_children doesn't mark the ticket FAILED, but
    # the review verdict path calls _merge_pr -> github_merge_pr tool
    # which hits real GitHub. Swap it for a no-op that returns True +
    # seeds state.merged_pr_urls with a fake SHA so restart-idempotency
    # bookkeeping is exercised in dry-run too.
    async def _dry_merge_pr(ticket: Any) -> bool:
        fake_sha = f"dryrun-sha-{adapter._counter}"
        try:
            ticket_id = getattr(ticket, "id", "") or ""
            state = getattr(orch, "state", None)
            if state is not None and ticket_id:
                state.merged_pr_urls[ticket_id] = fake_sha
        except Exception:
            logger.exception("dry-run _merge_pr bookkeeping failed")
        logger.info(
            "[DRY-RUN github] merge_pr id=%s sha=%s",
            getattr(ticket, "identifier", "?"), fake_sha,
        )
        return True

    orch._merge_pr = _dry_merge_pr  # type: ignore[assignment]

    # Stash on the orchestrator so tests / operators can reach the
    # adapter off the instance.
    orch._dry_run_adapter = adapter

    logger.info(
        "[autonomous_build] dry-run mode ACTIVE "
        "(auto_complete_after_seconds=%.2f)",
        adapter.auto_complete_after_seconds,
    )
    return adapter


def maybe_apply_dry_run(orch: Any) -> Optional[DryRunAdapter]:
    """Read the env flag and apply the adapter if set. Returns the adapter
    on activation, None otherwise.
    """
    if not dry_run_enabled():
        return None
    return apply_dry_run(orch)
