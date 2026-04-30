import pytest
from saluca_plugin_sdk.dataclasses import AgentCapabilities, RegistrationResult, AuditEvent
from datetime import datetime

@pytest.fixture
def sample_capabilities():
    return AgentCapabilities(supported_tasks=["task1", "task2"], max_concurrent=2)

@pytest.fixture
def sample_registration():
    return RegistrationResult(plugin_id="test-plugin", timestamp=datetime.utcnow())

@pytest.fixture
def sample_audit_event():
    return AuditEvent(event_type="test", payload={"key": "value"})
