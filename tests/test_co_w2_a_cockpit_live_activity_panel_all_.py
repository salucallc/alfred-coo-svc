import datetime
import pytest
from src.co_w2_a_cockpit_live_activity_panel_all_ import Subsystem, LiveActivityPanel

@pytest.fixture
def sample_subsystems():
    now = datetime.datetime.utcnow()
    subs = []
    for i in range(6):
        subs.append(
            Subsystem(
                session_id=f"session-{i:04d}",
                current_task=f"task-{i}",
                created_at=now - datetime.timedelta(seconds=10 * i),
                last_heartbeat=now - datetime.timedelta(seconds=2 * i),
                node_id=f"node-{i}",
                harness=f"harness-{i}",
                orchestrator=(i % 2 == 0),
            )
        )
    return subs

def test_render_at_least_five_rows(sample_subsystems):
    panel = LiveActivityPanel(subsystems=sample_subsystems, filter_mode=LiveActivityPanel.FILTER_ALL)
    rows = panel.render_rows()
    assert len(rows) >= 5
    # Verify each row contains required keys and truncated session_id
    for row in rows:
        assert set(row.keys()) == {"session_id", "current_task", "age", "last_heartbeat", "node_id", "harness"}
        assert len(row["session_id"]) == 8  # truncated to 8 chars

def test_filter_chips_subs(sample_subsystems):
    panel = LiveActivityPanel(subsystems=sample_subsystems, filter_mode=LiveActivityPanel.FILTER_SUBS)
    rows = panel.render_rows()
    # Only non‑orchestrator subs (odd indices) should be included
    expected = [s for s in sample_subsystems if not s.orchestrator][:5]
    assert len(rows) == len(expected)
    for row, sub in zip(rows, expected):
        assert row["session_id"] == sub.session_id[:8]

def test_filter_chips_orchestrators(sample_subsystems):
    panel = LiveActivityPanel(subsystems=sample_subsystems, filter_mode=LiveActivityPanel.FILTER_ORCHESTRATORS)
    rows = panel.render_rows()
    expected = [s for s in sample_subsystems if s.orchestrator][:5]
    assert len(rows) == len(expected)
    for row, sub in zip(rows, expected):
        assert row["session_id"] == sub.session_id[:8]

def test_empty_state():
    panel = LiveActivityPanel(subsystems=[], filter_mode=LiveActivityPanel.FILTER_ALL)
    rows = panel.render_rows()
    assert rows == []

def test_poll_updates_changes(sample_subsystems):
    panel = LiveActivityPanel(subsystems=sample_subsystems, filter_mode=LiveActivityPanel.FILTER_ALL)
    first = panel.render_rows()
    # Simulate time passing by adjusting ages indirectly via created_at
    for sub in sample_subsystems:
        sub.created_at -= datetime.timedelta(seconds=5)
    panel.poll_update()
    second = panel._last_render
    assert first != second  # ages should have changed
