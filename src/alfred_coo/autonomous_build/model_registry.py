"""Model registry — hot-swappable model selection per role.

Sub #62 (2026-04-27). Cristian directive: registry pattern that's
hot-swappable, with reliability guardrails so updating the registry can't
break production.

This module owns:

* `_load_model_registry()` — reads `registry.yaml` from disk, validates
  schema, caches with mtime-based invalidation. On schema failure, falls
  back to the last-known-good cached version (does NOT crash dispatch).
* `_pick_model_for_role(role, attempt_n=0)` — picks the model for a role
  given an attempt number. n=0 = primary, n=1+ = fallback chain index, and
  exhausting the chain returns `last_resort`. Optional `consecutive_hard_timeouts`
  argument triggers the auto-rollback to `stable_baseline` after 3 in a row.
* `record_hard_timeout(role)` / `record_success(role)` — tiny in-memory
  counter for the consecutive-hard-timeout circuit breaker. Per-role.

Ground rules:
  * Registry is the SOURCE for the fallback chain; the per-kickoff
    `model_routing.<role>` payload field is the per-run escape hatch
    (handled at the call site in main.py / orchestrator dispatch).
  * Schema-invalid registry NEVER takes production down. Worst case the
    daemon keeps running on the previous valid version with a WARN.
  * Auto-rollback to `stable_baseline` on 3 consecutive hard-timeouts
    sticks until the registry mtime changes (treated as operator intent).

Path resolution:
  * `MODEL_REGISTRY_PATH` env var wins. Daemon-side it points at
    `/etc/alfred-coo/model_registry.yaml` on Oracle (synced from
    `Z:/_planning/model_registry/registry.yaml` on minipc).
  * Falls back to the minipc planning path so a local dev session works
    without env wiring.

The module-level cache + counter state is intentional: the daemon is one
long-running process; per-role state must persist across dispatches
(otherwise the circuit breaker can't see "3 in a row").
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("alfred_coo.autonomous_build.model_registry")


# ── Path resolution ────────────────────────────────────────────────────────

# Env var takes precedence so the systemd unit can drop a path in
# `EnvironmentFile=`. Falls back to the minipc planning path for local dev.
_DEFAULT_REGISTRY_PATHS = [
    "/etc/alfred-coo/model_registry.yaml",
    "/opt/alfred-coo/model_registry/registry.yaml",
    "Z:/_planning/model_registry/registry.yaml",
]


def _resolve_registry_path() -> Optional[Path]:
    """Pick the first existing registry path.

    Search order: ``$MODEL_REGISTRY_PATH`` (if set), then the canonical
    locations on Oracle and the minipc planning dir. Returns None when
    none of them exist — callers treat this as "no registry available;
    use the kickoff-payload defaults".
    """
    env_path = os.environ.get("MODEL_REGISTRY_PATH")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p
        # Don't silently fall through if the operator set the env var: log
        # so the misconfig is visible, but DO fall through so production
        # stays up on the canonical paths.
        logger.warning(
            "MODEL_REGISTRY_PATH=%s does not exist; falling through to "
            "canonical paths",
            env_path,
        )
    for cand in _DEFAULT_REGISTRY_PATHS:
        p = Path(cand)
        if p.is_file():
            return p
    return None


# ── Schema validation ──────────────────────────────────────────────────────


# Required top-level keys.
_REQUIRED_KEYS = {"schema_version", "models", "roles", "stable_baseline"}
# Required per-role keys.
_REQUIRED_ROLE_KEYS = {"primary", "fallback_chain", "last_resort"}
# Currently-supported schema versions. Bumping this is a coordinated change:
# bump in registry.yaml AND here AND any consumers that key off shape.
_SUPPORTED_SCHEMA_VERSIONS = {1}


def _validate_registry(parsed: Any) -> None:
    """Raise ValueError on any schema violation. Returns None on success.

    Validation is strict-but-permissive: top-level + per-role required
    keys must exist with the right type, but the `models` block is treated
    as a free-form catalog (the daemon doesn't read its details — only
    role.primary / role.fallback_chain / role.last_resort matter for
    dispatch).
    """
    if not isinstance(parsed, dict):
        raise ValueError(
            f"registry root must be a mapping, got {type(parsed).__name__}"
        )
    missing = _REQUIRED_KEYS - parsed.keys()
    if missing:
        raise ValueError(
            f"registry missing required top-level keys: {sorted(missing)}"
        )
    sv = parsed.get("schema_version")
    if sv not in _SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(
            f"unsupported schema_version={sv!r}; supported: "
            f"{sorted(_SUPPORTED_SCHEMA_VERSIONS)}"
        )
    roles = parsed.get("roles")
    if not isinstance(roles, dict) or not roles:
        raise ValueError("registry.roles must be a non-empty mapping")
    for role_name, role_block in roles.items():
        if not isinstance(role_block, dict):
            raise ValueError(
                f"role {role_name!r} must be a mapping, got "
                f"{type(role_block).__name__}"
            )
        rmissing = _REQUIRED_ROLE_KEYS - role_block.keys()
        if rmissing:
            raise ValueError(
                f"role {role_name!r} missing required keys: {sorted(rmissing)}"
            )
        if not isinstance(role_block["primary"], str) or not role_block["primary"]:
            raise ValueError(f"role {role_name!r} primary must be a non-empty string")
        if not isinstance(role_block["fallback_chain"], list):
            raise ValueError(f"role {role_name!r} fallback_chain must be a list")
        if not isinstance(role_block["last_resort"], str) or not role_block["last_resort"]:
            raise ValueError(
                f"role {role_name!r} last_resort must be a non-empty string"
            )
    baseline = parsed.get("stable_baseline")
    if not isinstance(baseline, dict) or not baseline:
        raise ValueError("registry.stable_baseline must be a non-empty mapping")


# ── Cache ─────────────────────────────────────────────────────────────────


@dataclass
class _RegistryCache:
    """In-memory cache for the parsed registry + circuit-breaker counters.

    `mtime_ns` of -1 means "no successful load yet". `parsed` is None until
    the first successful load; a mid-life schema failure preserves the last
    successful `parsed` so dispatch keeps working.

    `consecutive_hard_timeouts` is per-role (key = role name). Reset on
    success or on registry mtime change.

    `auto_rollback_active` is the set of roles currently force-routed to
    stable_baseline because they tripped 3 hard-timeouts in a row. Cleared
    on registry mtime change.
    """

    parsed: Optional[Dict[str, Any]] = None
    mtime_ns: int = -1
    path: Optional[Path] = None
    consecutive_hard_timeouts: Dict[str, int] = field(default_factory=dict)
    auto_rollback_active: set = field(default_factory=set)
    # Lock guards mutation of all fields above. Reads are Python-atomic so
    # we only take the lock on writes / paired read-modify-write.
    lock: threading.Lock = field(default_factory=threading.Lock)


_cache = _RegistryCache()

# Hard-timeout streak that triggers auto-rollback to stable_baseline.
HARD_TIMEOUT_AUTO_ROLLBACK_THRESHOLD = 3


# ── Public API ────────────────────────────────────────────────────────────


def _load_model_registry(force: bool = False) -> Optional[Dict[str, Any]]:
    """Load + cache the registry. Re-reads when the file mtime changes.

    Returns the parsed registry dict, or None when no registry file exists
    on disk (fresh deploy with no registry yet). Schema failures preserve
    the cached version and log WARN — they NEVER raise.

    Set `force=True` to bypass the mtime cache (used by tests).
    """
    path = _resolve_registry_path()
    if path is None:
        # No registry on disk anywhere we look. Caller falls back to its
        # legacy model-selection path. Logged once at WARN so the operator
        # knows the registry isn't being honoured.
        if _cache.path is not None or _cache.mtime_ns != -1:
            # Was loaded before, file disappeared. Don't drop the cache —
            # keep serving the last good copy with a loud log.
            logger.warning(
                "model registry path went missing (last seen %s); "
                "continuing on cached copy",
                _cache.path,
            )
            return _cache.parsed
        return None

    # mtime check (skip when forced)
    try:
        cur_mtime = path.stat().st_mtime_ns
    except OSError as e:
        logger.warning("registry stat failed (%s): %s; using cache", path, e)
        return _cache.parsed

    if not force and cur_mtime == _cache.mtime_ns and _cache.parsed is not None:
        return _cache.parsed

    # Re-read.
    try:
        import yaml  # local import: keeps module import fast in test envs
        with open(path, "r", encoding="utf-8") as f:
            new_parsed = yaml.safe_load(f)
        _validate_registry(new_parsed)
    except Exception as e:  # noqa: BLE001 — broad catch is the point
        logger.warning(
            "registry validation failed: %s; falling back to cached version "
            "from path=%s mtime_ns=%d",
            e, _cache.path, _cache.mtime_ns,
        )
        return _cache.parsed

    # New successful load — install. mtime change is treated as operator
    # intervention: clear circuit-breaker state for every role.
    with _cache.lock:
        if _cache.mtime_ns != cur_mtime:
            _cache.consecutive_hard_timeouts.clear()
            _cache.auto_rollback_active.clear()
        _cache.parsed = new_parsed
        _cache.mtime_ns = cur_mtime
        _cache.path = path

    logger.info(
        "model registry loaded: path=%s schema_version=%s roles=%s",
        path, new_parsed.get("schema_version"),
        sorted((new_parsed.get("roles") or {}).keys()),
    )
    return _cache.parsed


# ── Plan M benchmark-score opt-in hook ────────────────────────────────────


# Role → (persona, task_type) mapping for the benchmark-pick hook. Add new
# rows as new roles are introduced. The fallback path is unchanged when a
# row is missing (returns None → static chain).
_ROLE_TO_PERSONA_TASK = {
    "build": ("alfred-coo-a", "builder"),
    "builder": ("alfred-coo-a", "builder"),
    "qa": ("hawkman-qa-a", "qa"),
    "review": ("hawkman-qa-a", "qa"),
    "kickoff": ("alfred-coo-a", "orchestrator"),
    "decompose": ("alfred-coo-a", "orchestrator"),
}


def _role_to_persona_task(role: str):
    pair = _ROLE_TO_PERSONA_TASK.get(role)
    if pair is None:
        return None, None
    return pair


def _benchmark_pick(role: str) -> Optional[str]:
    """Plan M selector hook — opt-in via ``USE_BENCHMARK_SCORES`` env var.

    When the flag is truthy, attempt to pick the best model for ``role``
    from the most recent benchmark scores in soul-svc memory. Failure
    cases (no env, no scores, no eligible model, soul-svc unreachable,
    asyncio surprises) all return None so the caller falls through to
    the static registry-based selection — Plan M §4.3 "warn before
    enforce" discipline.

    Role → (persona, task_type) mapping uses a small heuristic table; if
    callers want richer routing they pass the explicit pair through the
    kickoff payload and we never reach this hook. Keeping the mapping
    here means registry-only callers get one-line opt-in.
    """
    if os.environ.get("USE_BENCHMARK_SCORES", "").lower() not in {"1", "true", "yes"}:
        return None
    persona, task_type = _role_to_persona_task(role)
    if persona is None:
        return None
    try:  # everything below is best-effort
        import asyncio as _asyncio
        from alfred_coo.benchmark.selector import (  # local import
            NoEligibleModel,
            pick_best_model,
        )
        from alfred_coo.benchmark.storage import load_latest_scores  # local import
        from alfred_coo.soul import SoulClient  # local import
    except Exception as e:  # noqa: BLE001
        logger.debug("[benchmark-pick] import failed: %s", e)
        return None

    base_url = os.environ.get("SOUL_API_URL")
    api_key = os.environ.get("SOUL_API_KEY") or os.environ.get("SOUL_API_TOKEN")
    if not base_url or not api_key:
        logger.debug("[benchmark-pick] no soul-svc env; skipping")
        return None

    async def _go():
        client = SoulClient(
            base_url=base_url,
            api_key=api_key,
            session_id=os.environ.get("SOUL_SESSION_ID", "alfred-coo-benchmark"),
        )
        try:
            scores = await load_latest_scores(
                client, persona=persona, task_type=task_type, limit=50,
            )
        finally:
            await client.close()
        if not scores:
            return None
        try:
            return pick_best_model(persona, task_type, scores)
        except NoEligibleModel:
            return None

    try:
        # Don't fight an existing event loop; if one is running, bail.
        _asyncio.get_running_loop()
        logger.debug("[benchmark-pick] running inside event loop; skipping")
        return None
    except RuntimeError:
        pass

    try:
        return _asyncio.run(_go())
    except Exception as e:  # noqa: BLE001
        logger.debug("[benchmark-pick] runtime failure: %s", e)
        return None


def _pick_model_for_role(
    role: str,
    attempt_n: int = 0,
    *,
    registry: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Return the model id for a role at the given attempt number.

    `attempt_n=0` => primary
    `attempt_n=1` => fallback_chain[0]
    `attempt_n=k` => fallback_chain[k-1] (or last_resort if past the chain)

    Returns None when:
      * the registry is absent (no file on disk and no cached copy), OR
      * the role is unknown in the registry.

    The caller is expected to fall back to its legacy selection path on
    None (kickoff-payload model_routing or persona.preferred_model).

    Plan M opt-in: when ``USE_BENCHMARK_SCORES`` is set AND ``attempt_n=0``
    AND benchmark scores exist for the resolved (persona, task_type),
    return the score-winning model instead of the static primary. The
    static chain still serves attempts 1..N so a flaky benchmark-picked
    model can degrade through the regular fallback.

    Auto-rollback path: if `role` is currently in `auto_rollback_active`
    (3 consecutive hard-timeouts on its primary), this returns the
    `stable_baseline.<role>` regardless of `attempt_n` — until the next
    registry mtime change clears the rollback.
    """
    if attempt_n <= 0:
        bench_pick = _benchmark_pick(role)
        if bench_pick:
            logger.info(
                "[benchmark-pick] role=%s using score-winning model=%s",
                role, bench_pick,
            )
            return bench_pick

    reg = registry if registry is not None else _load_model_registry()
    if reg is None:
        return None

    # Auto-rollback short-circuit. Read with the lock to avoid races against
    # `record_hard_timeout` flipping the set mid-pick.
    with _cache.lock:
        rolled_back = role in _cache.auto_rollback_active
    if rolled_back:
        baseline_map = reg.get("stable_baseline") or {}
        baseline = baseline_map.get(role)
        if baseline:
            logger.warning(
                "[model-registry] auto-rollback active for role=%s; "
                "using stable_baseline=%s",
                role, baseline,
            )
            return baseline
        # No baseline configured — fall through to the regular chain. The
        # circuit breaker still records hard-timeouts but the operator hasn't
        # told us what the safe model is.
        logger.error(
            "[model-registry] role=%s in auto_rollback but stable_baseline "
            "is empty — falling through to regular chain",
            role,
        )

    roles = reg.get("roles") or {}
    role_block = roles.get(role)
    if role_block is None:
        return None

    if attempt_n <= 0:
        return role_block["primary"]
    chain: List[str] = list(role_block.get("fallback_chain") or [])
    idx = attempt_n - 1
    if idx < len(chain):
        return chain[idx]
    return role_block.get("last_resort") or role_block["primary"]


def record_hard_timeout(role: str) -> bool:
    """Increment the consecutive-hard-timeout counter for `role`.

    Returns True iff the threshold was just crossed and `role` was added
    to `auto_rollback_active`. Idempotent on subsequent crossings.
    """
    with _cache.lock:
        cur = _cache.consecutive_hard_timeouts.get(role, 0) + 1
        _cache.consecutive_hard_timeouts[role] = cur
        crossed = (
            cur >= HARD_TIMEOUT_AUTO_ROLLBACK_THRESHOLD
            and role not in _cache.auto_rollback_active
        )
        if crossed:
            _cache.auto_rollback_active.add(role)
    if crossed:
        logger.warning(
            "[model-registry] role=%s hit %d consecutive hard-timeouts; "
            "auto-rolling back to stable_baseline until next registry "
            "mtime change",
            role, cur,
        )
    return crossed


def record_success(role: str) -> None:
    """Reset the consecutive-hard-timeout counter for `role`.

    Does NOT clear auto_rollback_active — that only flips on registry
    mtime change so a fluky-but-recovering primary doesn't immediately
    re-promote itself before the operator has reviewed the bug log.
    """
    with _cache.lock:
        _cache.consecutive_hard_timeouts.pop(role, None)


def is_role_rolled_back(role: str) -> bool:
    """Return True iff `role` is currently force-routed to stable_baseline."""
    with _cache.lock:
        return role in _cache.auto_rollback_active


# ── Test helpers ──────────────────────────────────────────────────────────


def _reset_for_tests() -> None:
    """Wipe the module-level cache. Test-only."""
    with _cache.lock:
        _cache.parsed = None
        _cache.mtime_ns = -1
        _cache.path = None
        _cache.consecutive_hard_timeouts.clear()
        _cache.auto_rollback_active.clear()
