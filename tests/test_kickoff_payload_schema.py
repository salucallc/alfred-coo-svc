"""SAL-3922 · kickoff payload schema validation tests.

Covers each silent-default case observed 2026-05-02 plus the new
acceptance criteria from the SAL-3922 spec:

  1. ``budget_usd`` (flat typo) → WARNING + auto-migrate to ``budget.max_usd``.
  2. ``waves`` (wrong field name) → WARNING + auto-migrate to ``wave_order``.
  3. ``concurrency.max_parallel_subs`` (canonical) → silent, no warning.
  4. ``manual_kickoff`` / ``kickoff_origin`` / ``acked_*`` (informational
     fields) accepted without warnings.
  5. Existing kickoff-payload tests in
     ``tests/test_autonomous_build_orchestrator.py`` still pass without
     modification (covered by `pytest tests/test_autonomous_build_orchestrator.py`).
  6. Unknown top-level field rejects with a hint naming the offender.
  7. Nested-config typos rejected via pydantic ``extra='forbid'``.
"""

from __future__ import annotations

import logging

import pytest

from alfred_coo.autonomous_build.kickoff_schema import (
    KickoffPayload,
    validate_and_normalize_kickoff_payload,
)

# Reuse the orchestrator-builder fixture from the main test module so the
# end-to-end ``_parse_payload`` paths stay covered.
from tests.test_autonomous_build_orchestrator import _mk_orchestrator


# ── flat-typo auto-migration ───────────────────────────────────────────────


def test_budget_usd_flat_typo_auto_migrates_with_warning(caplog):
    """Acceptance #1: ``budget_usd: 80.0`` (flat typo) must emit a WARNING
    with the fix hint and auto-migrate into ``budget.max_usd``.
    """
    payload = {"linear_project_id": "p1", "budget_usd": 80.0}
    with caplog.at_level(logging.WARNING):
        out = validate_and_normalize_kickoff_payload(payload)
    assert out == {"linear_project_id": "p1", "budget": {"max_usd": 80.0}}
    msgs = [r.getMessage() for r in caplog.records]
    assert any("budget_usd" in m and "budget.max_usd" in m for m in msgs), (
        f"WARNING must name flat key + canonical location; got {msgs!r}"
    )


def test_waves_wrong_field_name_auto_migrates_with_warning(caplog):
    """Acceptance #2: ``waves: [3, 4, 5]`` (wrong field name) must emit a
    WARNING with the ``wave_order`` hint and auto-migrate.
    """
    payload = {"linear_project_id": "p1", "waves": [3, 4, 5]}
    with caplog.at_level(logging.WARNING):
        out = validate_and_normalize_kickoff_payload(payload)
    assert out == {"linear_project_id": "p1", "wave_order": [3, 4, 5]}
    msgs = [r.getMessage() for r in caplog.records]
    assert any("waves" in m and "wave_order" in m for m in msgs), (
        f"WARNING must name wrong field + canonical name; got {msgs!r}"
    )


def test_max_parallel_subs_flat_typo_auto_migrates(caplog):
    """``max_parallel_subs`` at top level must auto-migrate into
    ``concurrency.max_parallel_subs`` with a WARNING.
    """
    payload = {"linear_project_id": "p1", "max_parallel_subs": 12}
    with caplog.at_level(logging.WARNING):
        out = validate_and_normalize_kickoff_payload(payload)
    assert out == {
        "linear_project_id": "p1",
        "concurrency": {"max_parallel_subs": 12},
    }
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "max_parallel_subs" in m and "concurrency" in m for m in msgs
    ), msgs


def test_per_epic_cap_flat_typo_auto_migrates(caplog):
    """``per_epic_cap`` at top level must auto-migrate into
    ``concurrency.per_epic_cap`` with a WARNING.
    """
    payload = {"linear_project_id": "p1", "per_epic_cap": 5}
    with caplog.at_level(logging.WARNING):
        out = validate_and_normalize_kickoff_payload(payload)
    assert out == {
        "linear_project_id": "p1",
        "concurrency": {"per_epic_cap": 5},
    }


def test_status_cadence_min_flat_typo_auto_migrates(caplog):
    """``status_cadence_min`` at top level must auto-migrate into
    ``status_cadence.interval_minutes`` with a WARNING.
    """
    payload = {"linear_project_id": "p1", "status_cadence_min": 15}
    with caplog.at_level(logging.WARNING):
        out = validate_and_normalize_kickoff_payload(payload)
    assert out == {
        "linear_project_id": "p1",
        "status_cadence": {"interval_minutes": 15},
    }


def test_flat_typo_merges_into_existing_nested_dict(caplog):
    """If the operator writes BOTH the canonical nested form AND a flat
    typo for a SIBLING field (different key), the flat typo merges into
    the existing nested dict without clobbering the canonical sibling.
    """
    payload = {
        "linear_project_id": "p1",
        "concurrency": {"max_parallel_subs": 8},
        "per_epic_cap": 3,  # flat typo, sibling field
    }
    with caplog.at_level(logging.WARNING):
        out = validate_and_normalize_kickoff_payload(payload)
    assert out["concurrency"] == {"max_parallel_subs": 8, "per_epic_cap": 3}


def test_flat_typo_with_canonical_present_keeps_canonical(caplog):
    """If the operator writes BOTH ``budget.max_usd`` (canonical) AND
    ``budget_usd`` (flat typo) for the same field, keep the canonical
    value and drop the flat typo with a WARNING.
    """
    payload = {
        "linear_project_id": "p1",
        "budget": {"max_usd": 50.0},
        "budget_usd": 9999.0,
    }
    with caplog.at_level(logging.WARNING):
        out = validate_and_normalize_kickoff_payload(payload)
    assert out["budget"]["max_usd"] == 50.0
    assert "budget_usd" not in out


def test_waves_with_canonical_wave_order_keeps_canonical(caplog):
    """``waves`` is dropped if ``wave_order`` is also present."""
    payload = {
        "linear_project_id": "p1",
        "wave_order": [0, 1, 2],
        "waves": [9, 9, 9],
    }
    with caplog.at_level(logging.WARNING):
        out = validate_and_normalize_kickoff_payload(payload)
    assert out["wave_order"] == [0, 1, 2]
    assert "waves" not in out


# ── canonical form silent-pass ─────────────────────────────────────────────


def test_canonical_nested_form_parses_silently_no_warnings(caplog):
    """Acceptance #3: a payload using the canonical nested form must
    parse without emitting any WARNING records.
    """
    payload = {
        "linear_project_id": "p1",
        "concurrency": {"max_parallel_subs": 6, "per_epic_cap": 3},
        "budget": {"max_usd": 30.0},
        "status_cadence": {"interval_minutes": 20},
        "wave_order": [0, 1, 2, 3],
    }
    with caplog.at_level(logging.WARNING):
        out = validate_and_normalize_kickoff_payload(payload)
    assert out == payload  # unchanged
    warnings = [
        r for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert warnings == [], (
        f"canonical form must produce zero warnings; got {warnings!r}"
    )


def test_informational_fields_accepted_without_warnings(caplog):
    """Acceptance #4: informational fields (``manual_kickoff``,
    ``kickoff_origin``, ``kickoff_reason``, ``acked_*``, ``on_all_green``)
    must be accepted without warnings.
    """
    payload = {
        "linear_project_id": "p1",
        "manual_kickoff": True,
        "kickoff_origin": "slack-/v1ga-kickoff",
        "kickoff_reason": "wave-2 retry after green-ratio dip",
        "acked_by_user_id": "U0AH88KHZ4H",
        "ack_message_ts": "1714665600.000100",
        "ack_message_text": ":green-checkmark: kickoff",
        "acked_at": "2026-05-02T12:00:00Z",
        "on_all_green": ["tag v1.0.0-rc.7"],
    }
    with caplog.at_level(logging.WARNING):
        out = validate_and_normalize_kickoff_payload(payload)
    # Informational fields preserved.
    assert out["manual_kickoff"] is True
    assert out["kickoff_origin"] == "slack-/v1ga-kickoff"
    assert out["acked_by_user_id"] == "U0AH88KHZ4H"
    assert out["on_all_green"] == ["tag v1.0.0-rc.7"]
    warnings = [
        r for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert warnings == [], (
        f"informational fields must produce zero warnings; got {warnings!r}"
    )


# ── unknown-key rejection ──────────────────────────────────────────────────


def test_unknown_top_level_key_rejected_with_hint():
    """Truly unknown top-level keys with a known hint mapping (e.g.
    ``max_subs``) must be rejected with a clear ``RuntimeError``
    naming the offender + the canonical field.
    """
    payload = {"linear_project_id": "p1", "max_subs": 10}
    with pytest.raises(RuntimeError, match=r"max_subs.*concurrency\.max_parallel_subs"):
        validate_and_normalize_kickoff_payload(payload)


def test_unknown_top_level_key_no_hint_still_rejected():
    """Unknown top-level keys with no hint mapping must still be rejected
    (the hint is just a nicety on top of the rejection).
    """
    payload = {"linear_project_id": "p1", "totally_made_up_field": 42}
    with pytest.raises(RuntimeError, match=r"totally_made_up_field"):
        validate_and_normalize_kickoff_payload(payload)


def test_unknown_key_warning_mode_does_not_raise(caplog):
    """``raise_on_unknown=False`` (Option B / lighter touch) must
    downgrade the rejection to a WARNING so legacy callers can opt
    into the warning-only behaviour.
    """
    payload = {"linear_project_id": "p1", "max_subs": 10}
    with caplog.at_level(logging.WARNING):
        out = validate_and_normalize_kickoff_payload(
            payload, raise_on_unknown=False
        )
    # Unknown key dropped (we can't consume it), but no exception.
    assert "max_subs" not in out
    msgs = [r.getMessage() for r in caplog.records]
    assert any("max_subs" in m for m in msgs)


# ── nested-typo rejection (pydantic extra='forbid') ────────────────────────


def test_nested_budget_typo_rejected():
    """A typo INSIDE ``budget`` (e.g. ``max_usd_usd``) must be rejected
    by pydantic ``extra='forbid'`` on ``BudgetConfig``.
    """
    payload = {
        "linear_project_id": "p1",
        "budget": {"max_usd_usd": 80.0},  # typo inside nested
    }
    with pytest.raises(RuntimeError, match=r"max_usd_usd"):
        validate_and_normalize_kickoff_payload(payload)


def test_nested_concurrency_typo_rejected():
    """Typo inside ``concurrency`` rejected."""
    payload = {
        "linear_project_id": "p1",
        "concurrency": {"max_parallel_sbus": 10},  # ::shrug::
    }
    with pytest.raises(RuntimeError, match=r"max_parallel_sbus"):
        validate_and_normalize_kickoff_payload(payload)


def test_nested_status_cadence_typo_rejected():
    """Typo inside ``status_cadence`` rejected."""
    payload = {
        "linear_project_id": "p1",
        "status_cadence": {"interval_min": 20},  # forgot the "utes"
    }
    with pytest.raises(RuntimeError, match=r"interval_min"):
        validate_and_normalize_kickoff_payload(payload)


# ── orchestrator integration ───────────────────────────────────────────────


def test_parse_payload_auto_migrates_budget_usd_typo(caplog):
    """End-to-end: an orchestrator built with ``budget_usd`` (flat typo)
    must call into the validator, see the WARNING, and end up with
    ``budget_usd`` set to the migrated value (80.0) on the orchestrator,
    NOT the silent default ``$30``.
    """
    orch = _mk_orchestrator(kickoff_desc={
        "linear_project_id": "p1",
        "budget_usd": 80.0,
    })
    with caplog.at_level(logging.WARNING):
        orch._parse_payload()
    assert orch.budget_usd == pytest.approx(80.0), (
        "flat-typo budget_usd should auto-migrate, not default to "
        f"$30; got {orch.budget_usd}"
    )
    msgs = [r.getMessage() for r in caplog.records]
    assert any("budget_usd" in m for m in msgs)


def test_parse_payload_auto_migrates_waves_typo(caplog):
    """End-to-end: ``waves: [3, 4, 5]`` must surface as
    ``orch.wave_order == [3, 4, 5]``, not the silent default ``[0,1,2,3]``.

    This is the exact bug that bit Agent-Ingest twice today.
    """
    orch = _mk_orchestrator(kickoff_desc={
        "linear_project_id": "p1",
        "waves": [3, 4, 5],
    })
    with caplog.at_level(logging.WARNING):
        orch._parse_payload()
    assert orch.wave_order == [3, 4, 5], (
        f"waves typo should auto-migrate to wave_order; got {orch.wave_order}"
    )


def test_parse_payload_auto_migrates_max_parallel_subs_typo():
    """End-to-end: top-level ``max_parallel_subs`` must auto-migrate."""
    orch = _mk_orchestrator(kickoff_desc={
        "linear_project_id": "p1",
        "max_parallel_subs": 12,
    })
    orch._parse_payload()
    assert orch.max_parallel_subs == 12


def test_parse_payload_auto_migrates_per_epic_cap_typo():
    """End-to-end: top-level ``per_epic_cap`` must auto-migrate."""
    orch = _mk_orchestrator(kickoff_desc={
        "linear_project_id": "p1",
        "per_epic_cap": 5,
    })
    orch._parse_payload()
    assert orch.per_epic_cap == 5


def test_parse_payload_auto_migrates_status_cadence_min_typo():
    """End-to-end: top-level ``status_cadence_min`` must auto-migrate."""
    orch = _mk_orchestrator(kickoff_desc={
        "linear_project_id": "p1",
        "status_cadence_min": 15,
    })
    orch._parse_payload()
    assert orch.status_cadence_min == 15


def test_parse_payload_canonical_form_unchanged_behaviour():
    """End-to-end: canonical-form payload behaves exactly as before
    (no regression on the existing happy path).
    """
    orch = _mk_orchestrator(kickoff_desc={
        "linear_project_id": "p1",
        "concurrency": {"max_parallel_subs": 8, "per_epic_cap": 4},
        "budget": {"max_usd": 50.0},
        "status_cadence": {"interval_minutes": 30},
        "wave_order": [0, 1],
    })
    orch._parse_payload()
    assert orch.linear_project_id == "p1"
    assert orch.max_parallel_subs == 8
    assert orch.per_epic_cap == 4
    assert orch.budget_usd == pytest.approx(50.0)
    assert orch.status_cadence_min == 30
    assert orch.wave_order == [0, 1]


def test_parse_payload_unknown_field_raises():
    """End-to-end: a truly-unknown top-level field must surface as a
    ``RuntimeError`` from ``_parse_payload``, not silent default.
    """
    orch = _mk_orchestrator(kickoff_desc={
        "linear_project_id": "p1",
        "totally_made_up_field": 42,
    })
    with pytest.raises(RuntimeError, match=r"totally_made_up_field"):
        orch._parse_payload()


def test_parse_payload_empty_description_unchanged():
    """An empty kickoff description (legacy / test fixtures) still
    parses without invoking the validator.
    """
    orch = _mk_orchestrator(kickoff_desc="")
    # Empty payload triggers the existing missing-linear_project_id
    # error path, NOT a schema error.
    with pytest.raises(RuntimeError, match=r"linear_project_id"):
        orch._parse_payload()


def test_parse_payload_invalid_json_falls_back_to_empty():
    """Non-JSON kickoff descriptions log a WARNING and fall through
    to the existing missing-linear_project_id check, not a schema
    error. Backward-compat with the legacy parser.
    """
    orch = _mk_orchestrator(kickoff_desc="this is not json")
    with pytest.raises(RuntimeError, match=r"linear_project_id"):
        orch._parse_payload()


# ── KickoffPayload model surface ────────────────────────────────────────────


def test_kickoff_payload_model_fields_cover_canonical_keys():
    """The model must define every canonical field the orchestrator
    consumes today, so unknown-key detection doesn't false-positive on
    a new field someone added without updating the schema.
    """
    fields = set(KickoffPayload.model_fields.keys())
    # spot-check the consumed-by-orchestrator surface
    for required in (
        "linear_project_id",
        "concurrency", "budget", "status_cadence",
        "wave_order", "wave_retry_budget",
        "wave_green_ratio_threshold",
        "slack_channel", "model_routing", "builder_fallback_chain",
        "plan_doc_urls", "retry_budget", "retry_backoff_sec",
        "deadlock_grace_sec",
    ):
        assert required in fields, (
            f"{required!r} missing from KickoffPayload schema; "
            "would cause unknown-key false-positive in production"
        )
