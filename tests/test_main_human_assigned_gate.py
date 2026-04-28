"""SAL-3038 / SAL-3070: enforce human-assigned + terminal-state gate on
the bare mesh-claim path in ``alfred_coo.main``.

PR #171 added a ``human-assigned`` skip inside the orchestrator's
wave-dispatch loop (``autonomous_build/orchestrator.py:3076-3084``) but
the daemon ALSO has a second dispatch path: the bare poll loop in
``main.py`` that calls ``mesh.list_pending`` + ``mesh.claim`` directly
and never hydrates a ``Ticket``. That path was bypassing the gate
entirely.

Real-world incidents:

* SAL-3038: 47 mesh tasks queued at 00:40-00:54 UTC; Cristian applied
  the ``human-assigned`` label later; the bare-claim loop kept consuming
  them and dispatched 22 zombie PRs at ~6 min each.
* SAL-3070: same pattern brewing as of 2026-04-28.

Tests cover the pure decision surface
(``_should_skip_for_human_or_terminal``) — the same predicate is now
called from BOTH dispatch paths, so this single test file pins the
shared contract.
"""

from __future__ import annotations

from alfred_coo.main import (
    HUMAN_ASSIGNED_LABEL,
    LINEAR_TERMINAL_STATES,
    _should_skip_for_human_or_terminal,
)


# ── _should_skip_for_human_or_terminal ──────────────────────────────────────


def test_human_assigned_label_triggers_skip():
    """(a) ``human-assigned`` label on a Backlog ticket → skip + mark failed.

    Mirrors the SAL-3038 incident: ticket is in Backlog (not terminal),
    but Cristian has flagged it for human investigation.
    """
    status = {
        "identifier": "SAL-3038",
        "labels": ["human-assigned", "wave-1", "epic:gate-fix"],
        "state": "Backlog",
    }
    should_skip, reason = _should_skip_for_human_or_terminal(status)
    assert should_skip is True
    assert reason == "human_assigned"


def test_human_assigned_label_case_insensitive():
    """The Linear API surfaces label names as-typed; matcher must be
    case-insensitive so a future "Human-Assigned" rename doesn't reopen
    the SAL-3038 gap."""
    status = {
        "identifier": "SAL-3038",
        "labels": ["Human-Assigned"],
        "state": "Backlog",
    }
    should_skip, reason = _should_skip_for_human_or_terminal(status)
    assert should_skip is True
    assert reason == "human_assigned"


def test_terminal_state_done_triggers_skip():
    """(b) state=``Done`` → skip + mark failed.

    Catches the race where a mesh task was queued, the ticket was
    merged + flipped to Done by the orchestrator, but the mesh task
    didn't get cancelled in time and re-surfaces on the next claim
    tick.
    """
    status = {
        "identifier": "SAL-3038",
        "labels": ["wave-1"],
        "state": "Done",
    }
    should_skip, reason = _should_skip_for_human_or_terminal(status)
    assert should_skip is True
    assert reason == "terminal_state:Done"


def test_terminal_state_cancelled_triggers_skip():
    status = {
        "identifier": "SAL-3070",
        "labels": [],
        "state": "Cancelled",
    }
    should_skip, reason = _should_skip_for_human_or_terminal(status)
    assert should_skip is True
    assert reason == "terminal_state:Cancelled"


def test_terminal_state_duplicate_triggers_skip():
    status = {
        "identifier": "SAL-3070",
        "labels": [],
        "state": "Duplicate",
    }
    should_skip, reason = _should_skip_for_human_or_terminal(status)
    assert should_skip is True
    assert reason == "terminal_state:Duplicate"


def test_normal_backlog_proceeds():
    """(c) normal Backlog ticket without ``human-assigned`` → proceed.

    The happy path. No skip, no reason, predicate is silent.
    """
    status = {
        "identifier": "SAL-3071",
        "labels": ["wave-1", "epic:autonomous-ops", "size-S"],
        "state": "Backlog",
    }
    should_skip, reason = _should_skip_for_human_or_terminal(status)
    assert should_skip is False
    assert reason is None


def test_in_progress_proceeds():
    """A ticket already In Progress is not terminal and not human-owned;
    re-dispatch is allowed (e.g. fix-round respawn)."""
    status = {
        "identifier": "SAL-3071",
        "labels": [],
        "state": "In Progress",
    }
    should_skip, reason = _should_skip_for_human_or_terminal(status)
    assert should_skip is False
    assert reason is None


def test_none_status_fails_open():
    """When ``linear_get_issue_status`` returns ``None`` (no API key,
    transport error, ticket not found) the predicate MUST fail-open so
    the daemon doesn't stall on Linear flakiness. Caller proceeds to
    dispatch."""
    should_skip, reason = _should_skip_for_human_or_terminal(None)
    assert should_skip is False
    assert reason is None


def test_non_dict_status_fails_open():
    """Defensive: if a caller hands us a string / list / int, fail-open
    rather than crash the poll loop."""
    for bogus in ("oops", [], 42, object()):
        should_skip, reason = _should_skip_for_human_or_terminal(bogus)  # type: ignore[arg-type]
        assert should_skip is False
        assert reason is None


def test_empty_labels_and_state_proceeds():
    """A ticket with no labels and a non-terminal state goes through."""
    status = {"identifier": "SAL-3071", "labels": [], "state": "Backlog"}
    should_skip, reason = _should_skip_for_human_or_terminal(status)
    assert should_skip is False
    assert reason is None


def test_label_list_with_non_string_entries_is_tolerated():
    """If Linear ever returns a malformed label node (None / dict
    leftover), don't crash — just skip non-string entries."""
    status = {
        "identifier": "SAL-3038",
        "labels": [None, {"name": "human-assigned"}, "human-assigned"],
        "state": "Backlog",
    }
    should_skip, reason = _should_skip_for_human_or_terminal(status)
    assert should_skip is True
    assert reason == "human_assigned"


def test_terminal_state_cap_variant():
    """Linear historically capitalises state names; predicate must be
    case-insensitive on state too. (Both ``"done"`` and ``"DONE"`` are
    valid stand-ins for the canonical ``"Done"``.)"""
    for variant in ("done", "DONE", "Done"):
        status = {"identifier": "SAL-3038", "labels": [], "state": variant}
        should_skip, reason = _should_skip_for_human_or_terminal(status)
        assert should_skip is True, f"{variant!r} should trigger skip"
        assert reason == f"terminal_state:{variant}"


# ── module constants ────────────────────────────────────────────────────────


def test_human_assigned_label_constant_matches_orchestrator():
    """The label name MUST match
    ``autonomous_build.orchestrator.HUMAN_ASSIGNED_LABEL`` so the two
    paths agree on a single source of truth. Drift here = the bug
    re-opens on the path that fell behind."""
    from alfred_coo.autonomous_build.orchestrator import (
        HUMAN_ASSIGNED_LABEL as ORCH_LABEL,
    )
    assert HUMAN_ASSIGNED_LABEL == ORCH_LABEL


def test_terminal_states_set_includes_canonical_three():
    """The spec calls out Done / Cancelled / Duplicate. We additionally
    accept the US spelling ``canceled`` because Linear has accepted both
    in the wild — pin it so a future trim doesn't silently regress."""
    assert "done" in LINEAR_TERMINAL_STATES
    assert "cancelled" in LINEAR_TERMINAL_STATES
    assert "duplicate" in LINEAR_TERMINAL_STATES
