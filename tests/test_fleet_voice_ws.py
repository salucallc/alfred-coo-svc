"""Tests for the fleet_voice WebSocket gateway (SAL-4002, P1.4).

Uses FastAPI's TestClient (Starlette under the hood) so we don't need
to bind a real port. Covers:

  * handshake auth: missing / wrong / correct bearer token
  * hello frame on connect
  * binary opus blob round-trip + accounting
  * control message ack-echo
  * malformed JSON rejected with `error` frame
  * admin sessions snapshot endpoint (auth + payload shape)
  * pong updates last_seen but emits nothing back

Tests are sync (TestClient.websocket_connect is a context manager
returning a sync interface), so no asyncio markers are needed.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from alfred_coo.fleet_voice import server as voice_server
from alfred_coo.fleet_voice.server import SESSIONS, app


@pytest.fixture(autouse=True)
def _reset_sessions():
    """Clear the in-memory session table between tests."""
    SESSIONS.clear()
    yield
    SESSIONS.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _connect(client: TestClient, token: str = "valid-key", device_id: str | None = None):
    """Open an authed WS connection and return the context manager.

    Caller is responsible for using `with` so the connection is closed.
    """
    headers = {"Authorization": f"Bearer {token}"}
    url = "/v1/fleet/voice"
    if device_id:
        url += f"?device_id={device_id}"
    return client.websocket_connect(url, headers=headers)


# ---------------------------------------------------------------------------
# Handshake auth
# ---------------------------------------------------------------------------


def test_handshake_rejects_missing_authorization(client: TestClient):
    """No Authorization header → server closes with 1008 after accept."""
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/v1/fleet/voice") as ws:
            ws.receive_text()  # should never get here
    assert excinfo.value.code == 1008


def test_handshake_rejects_wrong_token(client: TestClient):
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with _connect(client, token="bogus") as ws:
            ws.receive_text()
    assert excinfo.value.code == 1008


def test_handshake_accepts_valid_token_and_sends_hello(client: TestClient):
    with _connect(client, device_id="kitchen-puck") as ws:
        hello_raw = ws.receive_text()
        hello = json.loads(hello_raw)
        assert hello["type"] == "hello"
        assert hello["device_id"] == "kitchen-puck"
        assert hello["protocol_version"] == voice_server.PROTOCOL_VERSION
        assert isinstance(hello["session_id"], str) and len(hello["session_id"]) > 0
        assert hello["seq"] == 0


def test_handshake_assigns_anon_device_id_when_omitted(client: TestClient):
    with _connect(client) as ws:
        hello = json.loads(ws.receive_text())
        assert hello["device_id"].startswith("anon-")


# ---------------------------------------------------------------------------
# Frame protocol
# ---------------------------------------------------------------------------


def test_binary_opus_blob_round_trip_updates_counters(client: TestClient):
    """Server treats binary as opaque: counters bump, no echo back."""
    with _connect(client, device_id="puck-1") as ws:
        json.loads(ws.receive_text())  # consume hello
        # Send a fake Opus payload (gateway treats it as opaque).
        blob = bytes(range(256)) * 4  # 1024 bytes
        ws.send_bytes(blob)
        # Round-trip a control message after the binary so we can observe
        # accumulated counter state via the snapshot endpoint.
        ws.send_text(json.dumps({"type": "ping_test", "seq": 1}))
        ack_raw = ws.receive_text()
        ack = json.loads(ack_raw)
        assert ack["type"] == "ack"
        assert ack["seq"] == 1
        assert ack["echo_type"] == "ping_test"

    # After disconnect the session record persists with final counters.
    sessions = list(SESSIONS.values())
    assert len(sessions) == 1
    snap = sessions[0].snapshot()
    assert snap["frames_in"] == 2  # 1 binary + 1 text
    assert snap["bytes_in"] == 1024 + len(json.dumps({"type": "ping_test", "seq": 1}))


def test_control_hello_replies_with_welcome(client: TestClient):
    """Client-sent `hello` gets a `welcome` (not a generic ack)."""
    with _connect(client) as ws:
        json.loads(ws.receive_text())  # server hello
        ws.send_text(json.dumps({"type": "hello", "seq": 7}))
        reply = json.loads(ws.receive_text())
        assert reply["type"] == "welcome"
        assert reply["seq"] == 7
        assert reply["protocol_version"] == voice_server.PROTOCOL_VERSION


def test_control_pong_is_silent(client: TestClient):
    """`pong` updates last_seen but emits nothing; next msg gets ack'd."""
    with _connect(client) as ws:
        json.loads(ws.receive_text())  # server hello
        ws.send_text(json.dumps({"type": "pong", "seq": 99}))
        ws.send_text(json.dumps({"type": "noop", "seq": 100}))
        ack = json.loads(ws.receive_text())
        # The first reply we get must be the ack for seq=100, proving
        # the pong did not produce its own outbound frame.
        assert ack["type"] == "ack"
        assert ack["seq"] == 100


def test_malformed_json_rejected_with_error_frame(client: TestClient):
    with _connect(client) as ws:
        json.loads(ws.receive_text())  # hello
        ws.send_text("{not json")
        err = json.loads(ws.receive_text())
        assert err["type"] == "error"
        assert err["reason"] == "not_json"


def test_control_missing_type_rejected(client: TestClient):
    with _connect(client) as ws:
        json.loads(ws.receive_text())  # hello
        ws.send_text(json.dumps({"seq": 4}))
        err = json.loads(ws.receive_text())
        assert err["type"] == "error"
        assert err["reason"] == "missing_type"
        assert err["seq"] == 4


def test_control_missing_seq_rejected(client: TestClient):
    with _connect(client) as ws:
        json.loads(ws.receive_text())  # hello
        ws.send_text(json.dumps({"type": "noop"}))
        err = json.loads(ws.receive_text())
        assert err["type"] == "error"
        assert err["reason"] == "missing_or_invalid_seq"


def test_control_negative_seq_rejected(client: TestClient):
    with _connect(client) as ws:
        json.loads(ws.receive_text())  # hello
        ws.send_text(json.dumps({"type": "noop", "seq": -1}))
        err = json.loads(ws.receive_text())
        assert err["type"] == "error"
        assert err["reason"] == "missing_or_invalid_seq"


# ---------------------------------------------------------------------------
# Sessions admin endpoint
# ---------------------------------------------------------------------------


def test_sessions_endpoint_requires_admin_auth(client: TestClient):
    resp = client.get("/v1/fleet/voice/sessions")
    assert resp.status_code == 401
    resp = client.get(
        "/v1/fleet/voice/sessions",
        headers={"Authorization": "Bearer wrong-admin"},
    )
    assert resp.status_code == 401


def test_sessions_endpoint_returns_snapshot(client: TestClient):
    """Active connection must show up in the admin snapshot."""
    with _connect(client, device_id="test-puck") as ws:
        json.loads(ws.receive_text())  # hello
        ws.send_text(json.dumps({"type": "noop", "seq": 1}))
        json.loads(ws.receive_text())  # ack
        resp = client.get(
            "/v1/fleet/voice/sessions",
            headers={"Authorization": "Bearer admin-key"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        snap = body["sessions"][0]
        assert snap["device_id"] == "test-puck"
        assert snap["disconnect_at"] is None  # still connected
        assert snap["frames_in"] >= 1
        assert snap["frames_out"] >= 2  # hello + ack
        assert snap["last_seq_in"] == 1


def test_sessions_endpoint_health_check(client: TestClient):
    """`/healthz` is unauthenticated and reports active session count."""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "active_sessions": 0}


# ---------------------------------------------------------------------------
# Auth helper unit tests (cheap, deterministic)
# ---------------------------------------------------------------------------


def test_extract_bearer_handles_edge_cases():
    assert voice_server._extract_bearer(None) is None
    assert voice_server._extract_bearer("") is None
    assert voice_server._extract_bearer("Basic abc") is None
    assert voice_server._extract_bearer("Bearer ") is None
    assert voice_server._extract_bearer("Bearer abc") == "abc"
    assert voice_server._extract_bearer("Bearer  spaced  ") == "spaced"


def test_parse_control_happy_path():
    msg, err = voice_server._parse_control(json.dumps({"type": "x", "seq": 5}))
    assert err is None
    assert msg == {"type": "x", "seq": 5}


@pytest.mark.parametrize("raw,expected_err", [
    ("not json", "not_json"),
    ("[]", "not_object"),
    ("null", "not_object"),
    (json.dumps({"seq": 1}), "missing_type"),
    (json.dumps({"type": "", "seq": 1}), "missing_type"),
    (json.dumps({"type": "x"}), "missing_or_invalid_seq"),
    (json.dumps({"type": "x", "seq": "1"}), "missing_or_invalid_seq"),
    (json.dumps({"type": "x", "seq": -3}), "missing_or_invalid_seq"),
])
def test_parse_control_rejects_bad(raw: str, expected_err: str):
    msg, err = voice_server._parse_control(raw)
    assert msg is None
    assert err == expected_err
