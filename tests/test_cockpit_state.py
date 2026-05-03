from fastapi import FastAPI
from fastapi.testclient import TestClient
from datetime import datetime, timezone, timedelta
import pytest

# Import the router and helper from the implementation module
from src.co_w2_b_extend_v1_cockpit_state_with_act import router, get_mesh_sessions


def build_app():
    app = FastAPI()
    app.include_router(router)
    return app


def test_cockpit_state_filters(monkeypatch):
    # Prepare sample sessions
    now = datetime.now(timezone.utc)
    sample = [
        {"name": "agent-1", "status": "online", "created_at": now - timedelta(minutes=2)},
        {"name": "agent-2", "status": "offline", "created_at": now - timedelta(minutes=10)},
        {"name": "user-1", "status": "online", "created_at": now - timedelta(minutes=1)},
    ]
    monkeypatch.setattr("src.co_w2_b_extend_v1_cockpit_state_with_act.get_mesh_sessions", lambda: sample)
    client = TestClient(build_app())
    resp = client.get("/v1/cockpit/state")
    assert resp.status_code == 200
    data = resp.json()
    assert "subagents" in data
    assert len(data["subagents"]) == 1
    assert data["subagents"][0]["name"] == "agent-1"


def test_cockpit_state_cap(monkeypatch):
    # Generate 60 valid agent sessions within time window
    now = datetime.now(timezone.utc)
    sample = []
    for i in range(60):
        sample.append({
            "name": f"agent-{i}",
            "status": "online",
            "created_at": now - timedelta(seconds=i),
        })
    monkeypatch.setattr("src.co_w2_b_extend_v1_cockpit_state_with_act.get_mesh_sessions", lambda: sample)
    client = TestClient(build_app())
    resp = client.get("/v1/cockpit/state")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["subagents"]) == 50
    # Ensure the first and last entries are within the expected range
    assert data["subagents"][0]["name"] == "agent-0"
    assert data["subagents"][-1]["name"] == "agent-49"
