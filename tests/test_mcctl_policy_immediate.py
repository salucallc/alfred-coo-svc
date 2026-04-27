import pytest
from src.mcctl.commands.policy import push

def test_immediate_push_interrupts():
    result = push(immediate=True)
    assert result["interrupted"] is True
    assert result["requeue_reason"] == "policy_immediate"
    # Ensure the simulated interruption is fast (well under 5 seconds)
    assert result["elapsed_seconds"] < 5

def test_non_immediate_push_no_interrupt():
    result = push(immediate=False)
    assert result["interrupted"] is False
