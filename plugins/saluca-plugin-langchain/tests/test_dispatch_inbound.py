import pytest
from saluca_plugin_langchain.dispatch import dispatch_inbound

def test_dispatch_inbound_success():
    task = {"id": "task-1", "input": "test"}
    scope = {"user": "tester"}
    result = dispatch_inbound(task, scope)
    assert result.status == "ok"
    events = [e["event"] for e in result.audit_events]
    assert "dispatch_received" in events
    assert "tool_call" in events
