"""SAL-3922 · kickoff payload schema validation.

The autonomous-build orchestrator was silently defaulting six known fields
when operators wrote them with the wrong nesting level or wrong field
name. Concrete cases observed (2026-05-02):

| Operator wrote                  | Orchestrator reads             | Effect                                  |
|---------------------------------|--------------------------------|-----------------------------------------|
| ``budget_usd: 80.0``            | ``budget.max_usd``             | Silent default ``$30.0``                |
| ``max_parallel_subs: 6``        | ``concurrency.max_parallel_subs`` | Silent default ``6``                 |
| ``per_epic_cap: 3``             | ``concurrency.per_epic_cap``   | Silent default ``3``                    |
| ``status_cadence_min: 20``      | ``status_cadence.interval_minutes`` | Silent default ``20``              |
| ``waves: [...]``                | ``wave_order``                 | Silent default ``[0,1,2,3]``            |
| ``model_routing.builder``       | also ``builder_fallback_chain`` | divergent routes                       |

This module surfaces those cases at parse time. Strategy:

1. **Auto-migrate the six known flat typos into the canonical nested form**
   with a WARNING log (so existing operator muscle-memory keeps working
   while the warning trains the operator to use the canonical key).
2. **Reject any remaining unknown top-level keys** with a clear error
   that names the offending field and (when possible) suggests the
   correct location.
3. **Validate the canonical shape** via a Pydantic v2 model with
   ``extra='forbid'`` on every nested config so a typo inside e.g.
   ``budget`` (``max_usd_usd``) gets caught the same way.

The function returns a normalized ``dict`` that the existing parser in
``orchestrator._parse_payload`` can consume without further code changes
beyond the single call site.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger(__name__)


# ── Pydantic models ─────────────────────────────────────────────────────────


class ConcurrencyConfig(BaseModel):
    """``payload.concurrency`` — orchestrator concurrency caps."""

    model_config = ConfigDict(extra="forbid")

    max_parallel_subs: Optional[int] = None
    per_epic_cap: Optional[int] = None


class BudgetConfig(BaseModel):
    """``payload.budget`` — wave budget cap."""

    model_config = ConfigDict(extra="forbid")

    max_usd: Optional[float] = None


class CadenceConfig(BaseModel):
    """``payload.status_cadence`` — Slack status poster cadence."""

    model_config = ConfigDict(extra="forbid")

    interval_minutes: Optional[int] = None
    slack_channel: Optional[str] = None
    stall_threshold_sec: Optional[int] = None


class KickoffPayload(BaseModel):
    """Canonical kickoff payload schema.

    Nested configs use ``extra='forbid'`` so a typo inside e.g. ``budget``
    (``max_usd_usd``) raises a ``ValidationError``. The top-level model
    intentionally allows extras at this layer because we want our own
    pre-pass to deliver hint-rich error messages for the legacy flat
    typos before pydantic raises a generic ``extra forbidden`` error.
    Unknown top-level keys that survive the pre-pass are rejected by
    :func:`validate_and_normalize_kickoff_payload` itself, not the model.
    """

    # NOTE: top-level ``extra='allow'`` is intentional. Unknown-key
    # detection is done by :func:`validate_and_normalize_kickoff_payload`
    # itself (after auto-migration of known flat typos) so we can deliver
    # hint-rich error messages naming each offender + its canonical
    # location, which a generic pydantic ``extra forbidden`` error
    # cannot do as cleanly.
    #
    # NOTE: every consumed field is typed ``Any``. The orchestrator's
    # existing ``_parse_payload`` is the source of truth for
    # type-coercion-with-fallback semantics (e.g. non-numeric
    # ``wave_green_ratio_threshold`` swallows + WARNING + keeps default).
    # That contract is load-bearing — strict pydantic types here would
    # crash on values the existing parser intentionally tolerates. The
    # schema's job is field-name validation, not type validation.
    model_config = ConfigDict(extra="allow")

    # ``linear_project_id`` is required by orchestrator semantics, but we
    # leave it Optional in the schema so the orchestrator's existing
    # explicit check ("kickoff payload missing linear_project_id — cannot
    # build ticket graph") fires with its own load-bearing message rather
    # than a generic pydantic "field required" error.
    linear_project_id: Optional[str] = None

    # canonical nested configs (Any at the top level; nested ``extra='forbid'``
    # is enforced manually below for the ``dict`` case so we can warn on
    # nested typos without crashing on legacy parser fallback paths).
    concurrency: Optional[Any] = None
    budget: Optional[Any] = None
    status_cadence: Optional[Any] = None

    # canonical flat fields (type checking is intentionally permissive —
    # orchestrator does its own coercion + fallback).
    wave_order: Optional[Any] = None
    wave_retry_budget: Optional[Any] = None
    wave_green_ratio_threshold: Optional[Any] = None
    slack_channel: Optional[Any] = None
    model_routing: Optional[Any] = None
    builder_fallback_chain: Optional[Any] = None
    plan_doc_urls: Optional[Any] = None
    retry_budget: Optional[Any] = None
    retry_backoff_sec: Optional[Any] = None
    deadlock_grace_sec: Optional[Any] = None

    # informational fields — accepted, not consumed by orchestrator logic.
    # Daemon and operator tooling stamp these on the kickoff for audit
    # traceability; the orchestrator itself ignores them at parse time.
    manual_kickoff: Optional[Any] = None
    kickoff_origin: Optional[Any] = None
    kickoff_reason: Optional[Any] = None
    acked_by_user_id: Optional[Any] = None
    ack_message_ts: Optional[Any] = None
    ack_message_text: Optional[Any] = None
    acked_at: Optional[Any] = None

    # SAL-2870 wave-retry self-spawned-kickoff metadata. Emitted by
    # ``_queue_wave_retry_kickoff`` in orchestrator.py on every wave
    # failure that still has retry budget. Without these fields whitelisted,
    # SAL-3922's ``extra='forbid'`` validator dead-letters the retry
    # kickoff before its first dispatch, defeating the entire wave-retry
    # mechanism. Observed live 2026-05-02 23:16Z on AIO MC v1.1.0-rc1.
    parent_kickoff_task_id: Optional[Any] = None
    retry_reason: Optional[Any] = None
    retry_for_wave: Optional[Any] = None
    # ``on_all_green`` accepts either a list of action strings (the
    # current canonical form, e.g. ``["tag v1.0.0-rc.7"]``) or a list
    # of dicts (reserved for future structured actions). We type as
    # ``List[Any]`` so both shapes pass schema validation; the runtime
    # dispatcher in ``_run_on_all_green_actions`` handles each entry.
    on_all_green: Optional[Any] = None


# ── Flat-typo auto-migration table ──────────────────────────────────────────


# Each entry: flat-key → (nested-parent, nested-child). When the operator
# wrote the flat form, we rewrite the payload to the canonical nested form
# and emit a WARNING. This table is the single source of truth for what
# counts as "known typo we'll auto-migrate" vs "unknown key we reject".
_FLAT_TYPO_MIGRATIONS: Dict[str, tuple[str, str]] = {
    "budget_usd": ("budget", "max_usd"),
    "max_parallel_subs": ("concurrency", "max_parallel_subs"),
    "per_epic_cap": ("concurrency", "per_epic_cap"),
    "status_cadence_min": ("status_cadence", "interval_minutes"),
    "stall_threshold_sec": ("status_cadence", "stall_threshold_sec"),
}

# Field-name aliases (wrong name → right name, both flat at top level).
_FLAT_RENAMES: Dict[str, str] = {
    "waves": "wave_order",
}

# Nudges for unknown keys we don't auto-migrate but can still hint about.
# Maps an unknown key to the canonical field name the operator probably
# meant. Used purely to make the rejection error message helpful.
_UNKNOWN_KEY_HINTS: Dict[str, str] = {
    "max_subs": "concurrency.max_parallel_subs",
    "epic_cap": "concurrency.per_epic_cap",
    "budget_max": "budget.max_usd",
    "max_budget": "budget.max_usd",
    "cadence_min": "status_cadence.interval_minutes",
    "cadence_minutes": "status_cadence.interval_minutes",
    "interval_minutes": "status_cadence.interval_minutes",
    "wave_orders": "wave_order",
    "fallback_chain": "builder_fallback_chain",
    "plan_docs": "plan_doc_urls",
    "routing": "model_routing",
}


# ── Public API ──────────────────────────────────────────────────────────────


def validate_and_normalize_kickoff_payload(
    payload: Dict[str, Any],
    *,
    raise_on_unknown: bool = True,
) -> Dict[str, Any]:
    """Validate ``payload`` against :class:`KickoffPayload` and return a
    normalized dict the orchestrator's existing parser can consume.

    Behaviour:

    * **Flat-typo auto-migration.** Each entry in ``_FLAT_TYPO_MIGRATIONS``
      is moved into the canonical nested form when the operator wrote the
      flat form. A WARNING is logged with the suggested correct location.
    * **Field rename.** Each entry in ``_FLAT_RENAMES`` is rewritten in
      place (e.g. ``waves`` → ``wave_order``). A WARNING is logged.
    * **Unknown keys.** Any remaining top-level key that the canonical
      model doesn't define is reported with a clear ``RuntimeError``,
      naming the offending key and (when known) suggesting the canonical
      location. ``raise_on_unknown=False`` downgrades this to a WARNING
      log instead of a raise (Option B / lighter touch).
    * **Nested ``extra='forbid'``.** Pydantic catches typos inside
      ``budget`` / ``concurrency`` / ``status_cadence`` (e.g.
      ``max_usd_usd``) and raises ``RuntimeError``.

    The function does not consume any field — it just normalizes shape.
    The existing ``_parse_payload`` then reads ``budget.max_usd``,
    ``concurrency.max_parallel_subs`` etc. as it always has.
    """
    if not isinstance(payload, dict):
        raise TypeError(
            f"kickoff payload must be a dict, got {type(payload).__name__}"
        )

    # Work on a shallow copy so we don't mutate the caller's payload.
    out: Dict[str, Any] = dict(payload)

    # ── 1. Field renames (flat → flat) ──────────────────────────────────
    for wrong_name, right_name in _FLAT_RENAMES.items():
        if wrong_name in out and right_name not in out:
            logger.warning(
                "kickoff payload: unknown top-level field %r; "
                "did you mean %r? auto-migrating.",
                wrong_name, right_name,
            )
            out[right_name] = out.pop(wrong_name)
        elif wrong_name in out and right_name in out:
            # Both present — keep the canonical form, drop the typo with
            # a WARNING so the operator can clean up.
            logger.warning(
                "kickoff payload: both %r (typo) and %r (canonical) "
                "present; keeping canonical %r and dropping %r.",
                wrong_name, right_name, right_name, wrong_name,
            )
            out.pop(wrong_name)

    # ── 2. Flat → nested auto-migration ─────────────────────────────────
    for flat_key, (parent, child) in _FLAT_TYPO_MIGRATIONS.items():
        if flat_key not in out:
            continue
        flat_value = out.pop(flat_key)
        nested = out.get(parent)
        if not isinstance(nested, dict):
            nested = {}
            out[parent] = nested
        if child in nested:
            logger.warning(
                "kickoff payload: both flat %r=%r and nested %s.%s=%r "
                "present; keeping nested value and discarding flat typo.",
                flat_key, flat_value, parent, child, nested[child],
            )
        else:
            logger.warning(
                "kickoff payload: flat field %r is a typo for nested "
                "%s.%s; auto-migrating value %r. Please update your "
                "kickoff to use the canonical nested form.",
                flat_key, parent, child, flat_value,
            )
            nested[child] = flat_value

    # ── 3. Unknown top-level keys (after migrations) ────────────────────
    canonical_keys = set(KickoffPayload.model_fields.keys())
    unknown = [k for k in out.keys() if k not in canonical_keys]
    if unknown:
        # Render a helpful error/warning naming each offender + a hint.
        parts: List[str] = []
        for k in unknown:
            hint = _UNKNOWN_KEY_HINTS.get(k)
            if hint:
                parts.append(f"{k!r} (did you mean {hint!r}?)")
            else:
                parts.append(repr(k))
        msg = (
            "kickoff payload contains unknown top-level field(s): "
            + ", ".join(parts)
            + ". See alfred_coo.autonomous_build.kickoff_schema."
            "KickoffPayload for the canonical schema."
        )
        if raise_on_unknown:
            # Drop them out so the pydantic validate below doesn't double-
            # raise on the same fields with a less helpful message.
            for k in unknown:
                out.pop(k, None)
            raise RuntimeError(msg)
        logger.warning(msg)
        for k in unknown:
            out.pop(k, None)

    # ── 4. Nested extra-forbidden validation ────────────────────────────
    # Walk each nested config (``budget`` / ``concurrency`` / ``status_cadence``)
    # and pydantic-validate it against its strict model. We only care about
    # the ``extra_forbidden`` errors here — other type errors are handled
    # by the orchestrator's tolerant parser (which logs WARNINGs and falls
    # back to defaults). The schema's job at this layer is ONLY to catch
    # typo'd keys inside nested configs, not to crash on values the
    # existing parser intentionally tolerates.
    nested_models: Dict[str, type[BaseModel]] = {
        "concurrency": ConcurrencyConfig,
        "budget": BudgetConfig,
        "status_cadence": CadenceConfig,
    }
    nested_extra_errors: List[str] = []
    for nested_key, nested_model in nested_models.items():
        nested_value = out.get(nested_key)
        if not isinstance(nested_value, dict):
            continue  # legacy parser handles non-dict via fallback
        try:
            nested_model.model_validate(nested_value)
        except ValidationError as exc:
            for err in exc.errors():
                if err.get("type") != "extra_forbidden":
                    continue  # let orchestrator's parser handle type errors
                bad_key = err.get("loc", (nested_key,))[-1]
                allowed = ", ".join(sorted(nested_model.model_fields.keys()))
                nested_extra_errors.append(
                    f"{nested_key}.{bad_key} is not a known field; "
                    f"allowed keys: [{allowed}]"
                )

    if nested_extra_errors:
        raise RuntimeError(
            "kickoff payload schema validation failed:\n  - "
            + "\n  - ".join(nested_extra_errors)
        )

    return out


__all__ = [
    "KickoffPayload",
    "ConcurrencyConfig",
    "BudgetConfig",
    "CadenceConfig",
    "validate_and_normalize_kickoff_payload",
]
