"""Tests for the fleet_voice STT adapter (SAL-4003, P1.5).

Covers the four acceptance criteria from the ticket:

  1. Opus frames accumulate into the per-session buffer correctly.
  2. WebRTC VAD segmenter produces the expected number of segments
     against a synthetic silence-speech-silence pattern.
  3. The `transcript` JSON control frame is sent over the WS with the
     correct shape on a `start_utterance` / `end_utterance` drain.
  4. The soul-svc `/v1/overview/chat` POST is fired with the right
     payload (mocked HTTP); its reply emits as `assistant_text`.

OpenAI Whisper and soul-svc are mocked at the `stt.transcribe` and
`stt.post_to_soul` boundaries so no live network I/O happens. The
opuslib decode path is also mocked because the Windows test box has
no libopus DLL on PATH (per the SAL-4003 ticket constraints).

The fleet_voice tests have a known pytest-collection quirk on
Windows + Python 3.12.10 (silent zero-collection on the file alone).
This file uses `from alfred_coo.fleet_voice import server, stt`
top-level so a successful import counts as the smoke check; the
broader `pytest tests/ -k fleet_voice_stt` invocation works fine.
"""

from __future__ import annotations

import json
import struct
from typing import Any

import pytest
from fastapi.testclient import TestClient

from alfred_coo.fleet_voice import server as voice_server
from alfred_coo.fleet_voice import stt
from alfred_coo.fleet_voice.server import SESSIONS, app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_sessions():
    SESSIONS.clear()
    yield
    SESSIONS.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _connect(client: TestClient, token: str = "valid-key", device_id: str = "test-puck"):
    headers = {"Authorization": f"Bearer {token}"}
    return client.websocket_connect(
        f"/v1/fleet/voice?device_id={device_id}", headers=headers
    )


def _silence_pcm(ms: int, sample_rate: int = 16000) -> bytes:
    """`ms` of int16 mono silence at `sample_rate`."""
    samples = sample_rate * ms // 1000
    return b"\x00\x00" * samples


def _speech_like_pcm(ms: int, sample_rate: int = 16000) -> bytes:
    """`ms` of int16 mono pseudo-speech that WebRTC VAD will flag.

    Real speech is hard to synthesize but VAD is energy + spectral
    triggered; a 300 Hz square-wave at half-scale reliably trips
    aggressiveness=2. We use a deterministic pattern so the test is
    repeatable.
    """
    samples = sample_rate * ms // 1000
    period = sample_rate // 300
    out = bytearray()
    for i in range(samples):
        val = 16000 if (i // (period // 2)) % 2 == 0 else -16000
        out.extend(struct.pack("<h", val))
    return bytes(out)


# ---------------------------------------------------------------------------
# 1. Opus buffer accumulation
# ---------------------------------------------------------------------------


def test_binary_frames_accumulate_into_opus_buffer(client: TestClient):
    """Each inbound binary frame is appended to `session.opus_buffer`."""
    with _connect(client) as ws:
        json.loads(ws.receive_text())  # consume server hello
        ws.send_bytes(b"\xfa\x01frame-1")
        ws.send_bytes(b"\xfa\x02frame-2")
        ws.send_bytes(b"\xfa\x03frame-3")
        # round-trip a control message so we know the server has
        # processed all three binary frames before we close.
        ws.send_text(json.dumps({"type": "noop", "seq": 1}))
        json.loads(ws.receive_text())  # ack

    session = list(SESSIONS.values())[0]
    assert session.opus_buffer == [
        b"\xfa\x01frame-1",
        b"\xfa\x02frame-2",
        b"\xfa\x03frame-3",
    ]
    assert session.frames_in == 4  # 3 binary + 1 text


def test_buffer_drains_on_start_utterance(client: TestClient, monkeypatch):
    """`start_utterance` empties the buffer (drain trigger)."""
    # Stub the entire pipeline so the drain path is exercised end-to-end
    # but no actual decode / Whisper call happens.
    async def fake_transcribe(_pcm: bytes) -> str:
        return "hello alfred"

    async def fake_post_to_soul(_t: str, *, session_id: str | None = None) -> str:
        return "Good evening, sir."

    monkeypatch.setattr(stt, "decode_opus_buffer", lambda frames: b"\x00" * 32000)
    monkeypatch.setattr(stt, "segment_on_vad", lambda pcm, sr: [pcm])
    monkeypatch.setattr(stt, "transcribe", fake_transcribe)
    monkeypatch.setattr(stt, "post_to_soul", fake_post_to_soul)

    with _connect(client) as ws:
        json.loads(ws.receive_text())  # hello
        ws.send_bytes(b"opus-1")
        ws.send_bytes(b"opus-2")
        ws.send_text(json.dumps({"type": "start_utterance", "seq": 5}))
        # Expect: ack, transcript, assistant_text in that order.
        ack = json.loads(ws.receive_text())
        transcript = json.loads(ws.receive_text())
        assistant = json.loads(ws.receive_text())

    assert ack["type"] == "ack"
    assert ack["echo_type"] == "start_utterance"
    assert transcript["type"] == "transcript"
    assert transcript["text"] == "hello alfred"
    assert transcript["source"] == "start_utterance"
    assert assistant["type"] == "assistant_text"
    assert assistant["text"] == "Good evening, sir."

    session = list(SESSIONS.values())[0]
    assert session.opus_buffer == []  # drained


# ---------------------------------------------------------------------------
# 2. VAD segmentation
# ---------------------------------------------------------------------------


def test_segment_on_vad_silence_only_returns_empty():
    pcm = _silence_pcm(2000)
    assert stt.segment_on_vad(pcm, 16000) == []


def test_segment_on_vad_single_utterance_returns_one_segment():
    """One speech burst flanked by silence yields one segment."""
    pcm = _silence_pcm(500) + _speech_like_pcm(800) + _silence_pcm(500)
    segments = stt.segment_on_vad(pcm, 16000)
    assert len(segments) == 1
    # Segment must be all int16 frame-aligned bytes.
    assert len(segments[0]) % 2 == 0
    assert len(segments[0]) > 0


def test_segment_on_vad_two_utterances_split_by_long_silence():
    """Silence longer than MAX_GAP_MS splits one stream into two utterances."""
    long_gap_ms = stt.MAX_GAP_MS + 600  # well over the bridge threshold
    pcm = (
        _silence_pcm(300)
        + _speech_like_pcm(600)
        + _silence_pcm(long_gap_ms)
        + _speech_like_pcm(600)
        + _silence_pcm(300)
    )
    segments = stt.segment_on_vad(pcm, 16000)
    assert len(segments) == 2


def test_segment_on_vad_short_burst_dropped_as_too_brief():
    """Sub-MIN_SEGMENT_MS bursts (cough, click) get filtered out."""
    pcm = _silence_pcm(400) + _speech_like_pcm(80) + _silence_pcm(400)
    segments = stt.segment_on_vad(pcm, 16000)
    assert segments == []


def test_segment_on_vad_bridges_short_mid_sentence_pause():
    """Brief silence inside one utterance does not split it."""
    short_gap_ms = max(stt.MAX_GAP_MS - 100, 60)
    pcm = (
        _silence_pcm(300)
        + _speech_like_pcm(400)
        + _silence_pcm(short_gap_ms)
        + _speech_like_pcm(400)
        + _silence_pcm(300)
    )
    segments = stt.segment_on_vad(pcm, 16000)
    assert len(segments) == 1


# ---------------------------------------------------------------------------
# 3. Transcript control message shape
# ---------------------------------------------------------------------------


def test_transcript_frame_shape_matches_protocol(client: TestClient, monkeypatch):
    """`transcript` frame must carry `type`, `seq`, `text`, `source`, `segment_count`."""
    async def fake_transcribe(_pcm: bytes) -> str:
        return "test transcript"

    async def fake_post_to_soul(_t: str, *, session_id: str | None = None) -> str:
        return ""  # skip assistant_text for this test

    monkeypatch.setattr(stt, "decode_opus_buffer", lambda frames: b"\x00" * 16000)
    monkeypatch.setattr(stt, "segment_on_vad", lambda pcm, sr: [pcm[:8000], pcm[8000:]])
    monkeypatch.setattr(stt, "transcribe", fake_transcribe)
    monkeypatch.setattr(stt, "post_to_soul", fake_post_to_soul)

    with _connect(client) as ws:
        json.loads(ws.receive_text())  # hello
        ws.send_bytes(b"opus-payload")
        ws.send_text(json.dumps({"type": "end_utterance", "seq": 42}))
        ack = json.loads(ws.receive_text())
        transcript = json.loads(ws.receive_text())

    assert ack["type"] == "ack" and ack["seq"] == 42
    assert set(transcript.keys()) >= {"type", "seq", "text", "source", "segment_count"}
    assert transcript["type"] == "transcript"
    assert transcript["text"] == "test transcript"
    assert transcript["source"] == "end_utterance"
    assert transcript["segment_count"] == 2
    assert isinstance(transcript["seq"], int) and transcript["seq"] >= 1


def test_drain_with_empty_buffer_is_silent_noop(client: TestClient, monkeypatch):
    """`start_utterance` with no audio buffered must not emit a transcript."""
    monkeypatch.setattr(stt, "decode_opus_buffer", lambda frames: b"")
    # Should not be called, but stub anyway so a regression surfaces.
    monkeypatch.setattr(stt, "transcribe", lambda *_: pytest.fail("transcribe called"))

    with _connect(client) as ws:
        json.loads(ws.receive_text())  # hello
        ws.send_text(json.dumps({"type": "start_utterance", "seq": 1}))
        # Only ack, then a noop ack to bound the receive.
        ack = json.loads(ws.receive_text())
        ws.send_text(json.dumps({"type": "noop", "seq": 2}))
        next_msg = json.loads(ws.receive_text())

    assert ack["echo_type"] == "start_utterance"
    assert next_msg["type"] == "ack"
    assert next_msg["echo_type"] == "noop"


def test_transcribe_failure_emits_error_frame(client: TestClient, monkeypatch):
    """Pipeline exception surfaces as `error` not silent loss.

    Replaces `logger.exception` with `logger.warning` for this test
    because pytest 8.4.2 + pluggy 1.6.0 on Python 3.12.10 (this minipc)
    hits a known `traceback_exception_init() got an unexpected keyword
    argument 'compact'` bug when capturing the traceback inside
    Starlette's WS receive loop. Production behaviour is unchanged;
    we only lose the traceback in the captured log for this one test.
    """
    monkeypatch.setattr(voice_server.logger, "exception", voice_server.logger.warning)

    async def boom(_pcm: bytes) -> str:
        raise RuntimeError("whisper exploded")

    monkeypatch.setattr(stt, "decode_opus_buffer", lambda frames: b"\x00" * 16000)
    monkeypatch.setattr(stt, "segment_on_vad", lambda pcm, sr: [pcm])
    monkeypatch.setattr(stt, "transcribe", boom)

    with _connect(client) as ws:
        json.loads(ws.receive_text())  # hello
        ws.send_bytes(b"opus-payload")
        ws.send_text(json.dumps({"type": "start_utterance", "seq": 7}))
        ack = json.loads(ws.receive_text())
        err = json.loads(ws.receive_text())

    assert ack["type"] == "ack"
    assert err["type"] == "error"
    assert err["reason"] == "stt_drain_failed"
    assert "whisper exploded" in err["detail"]


# ---------------------------------------------------------------------------
# 4. soul-svc HTTP payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_to_soul_sends_correct_payload(monkeypatch):
    """Verify the URL, headers, and JSON body sent to soul-svc."""
    captured: dict[str, Any] = {}

    class FakeResponse:
        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

        def raise_for_status(self):
            pass

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url, *, headers, json):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse({"response": "Yes, sir."})

    monkeypatch.setattr(stt, "SOUL_API_KEY", "test-soul-key")
    monkeypatch.setattr(stt, "SOUL_API_URL", "http://test-soul:8080")
    monkeypatch.setattr(stt.httpx, "AsyncClient", FakeAsyncClient)

    reply = await stt.post_to_soul("what time is it?", session_id="kitchen-puck")

    assert reply == "Yes, sir."
    assert captured["url"] == "http://test-soul:8080/v1/overview/chat"
    assert captured["headers"]["Authorization"] == "Bearer test-soul-key"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["json"] == {
        "session_id": "kitchen-puck",
        "message": "what time is it?",
    }


@pytest.mark.asyncio
async def test_post_to_soul_handles_empty_input():
    assert await stt.post_to_soul("") == ""


@pytest.mark.asyncio
async def test_post_to_soul_no_key_returns_empty(monkeypatch):
    monkeypatch.setattr(stt, "SOUL_API_KEY", "")
    assert await stt.post_to_soul("hi") == ""


@pytest.mark.asyncio
async def test_post_to_soul_field_name_tolerance(monkeypatch):
    """Reply field can be `response`, `text`, `message`, `reply`, or `content`."""
    captured_body = {"text": "via text field"}

    class FakeResponse:
        def json(self):
            return captured_body

        def raise_for_status(self):
            pass

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, *_, **__):
            return FakeResponse()

    monkeypatch.setattr(stt, "SOUL_API_KEY", "k")
    monkeypatch.setattr(stt.httpx, "AsyncClient", FakeAsyncClient)

    assert await stt.post_to_soul("hi") == "via text field"


# ---------------------------------------------------------------------------
# Whisper transcribe boundary (mocked HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcribe_posts_to_openai(monkeypatch):
    captured: dict[str, Any] = {}

    class FakeResponse:
        def json(self):
            return {"text": "  hello world  "}

        def raise_for_status(self):
            pass

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url, *, headers, files, data):
            captured["url"] = url
            captured["headers"] = headers
            captured["files"] = files
            captured["data"] = data
            return FakeResponse()

    monkeypatch.setattr(stt, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(stt.httpx, "AsyncClient", FakeAsyncClient)

    pcm = b"\x00\x00" * 1000
    text = await stt.transcribe(pcm)
    assert text == "hello world"
    assert captured["url"] == stt.OPENAI_STT_URL
    assert captured["headers"] == {"Authorization": "Bearer sk-test"}
    assert captured["data"]["model"] == stt.WHISPER_MODEL
    # WAV header sniff: file payload should start with "RIFF" + size + "WAVE".
    wav_blob = captured["files"]["file"][1]
    assert wav_blob[:4] == b"RIFF"
    assert wav_blob[8:12] == b"WAVE"


@pytest.mark.asyncio
async def test_transcribe_empty_pcm_returns_empty():
    assert await stt.transcribe(b"") == ""


@pytest.mark.asyncio
async def test_transcribe_no_key_returns_empty(monkeypatch):
    monkeypatch.setattr(stt, "OPENAI_API_KEY", "")
    assert await stt.transcribe(b"\x00" * 100) == ""
