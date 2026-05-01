import pytest
from saluca_plugin_langchain.discovery import discover_agent

class DummyAgent:
    name = "dummy-agent"
    tools = ["example_tool"]

def test_discovery_returns_capabilities():
    caps = discover_agent(DummyAgent())
    assert "capabilities" in caps
    assert caps["capabilities"]["name"] == "dummy-agent"
    assert caps["capabilities"]["supports_tool_calls"] is True
