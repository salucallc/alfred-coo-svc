# tests/test_fleet_reconcile.py
"""Tests for the reconcile endpoint implementation."""

from alfred_coo.fleet_endpoint.reconcile import reconcile

def test_reconcile_basic():
    result = reconcile(buffered_local_writes=200, buffered_hub_writes=150, last_global_seq=1000)
    assert result["reconciled"] is True
    # Duration should be capped at 60 seconds
    assert result["duration_seconds"] <= 60
    # Global sequence should be monotonic
    assert result["new_global_seq"] == 1000 + 200 + 150

def test_reconcile_no_duplicates():
    # Simulate zero writes should still be monotonic
    result = reconcile(buffered_local_writes=0, buffered_hub_writes=0, last_global_seq=500)
    assert result["reconciled"] is True
    assert result["new_global_seq"] == 500
    assert result["duration_seconds"] == 0
