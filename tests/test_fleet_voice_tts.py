"""Tests for the fleet_voice TTS adapter (SAL-4005, P2.1).

Covers the four acceptance criteria from the ticket:

  1. OpenAI tts-1 is called with the correct request shape (model, voice,
     input, response_format) — no live API call, mocked at httpx.
  2. The WS receives `audio_start` then binary Opus frames then `audio_end`
     in that exact order; the envelopes' `seq` matches.
  3. Pre-encode framing is 20 ms (640 bytes at 16 kHz mono int16); the
     pure `_pcm_to_opus_frames` helper carves whole frames out of a known
     PCM blob and the encoder receives one frame per call.
  4. A TTS error path emits `{"type":"error","stage":"tts",...}` and does
     NOT crash the WS receive loop (subsequent control messages still ack).

Plus: `pyproject.toml` `[voice]` extra is unchanged so we did not silently
add a top-level dependency.

The opuslib encode path is mocked because the Windows test box has no
`libopus.dll` on PATH (per the SAL-4003 ticket constraints, mirrored here).
The OpenAI HTTP boundary is mocked the same way `tests/test_fleet_voice_stt`
mocks it — a fake `httpx.AsyncClient` with `.stream(...)` context manager.
"""

from __future__ import annotations

import json
import struct
import tomllib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from alfred_coo.fleet_voice import server as voice_server
from alfred_coo.fleet_voice import stt
from alfred_coo.fleet_voice import tts
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


# Stub Opus encoder used in tests so we don't need libopus on the box.
class _StubOpusEncoder:
    """Records every encode call; returns a deterministic packet per frame."""

    instances: list["_StubOpusEncoder"] = []

    def __init__(self, sample_rate, channels, application):
        self.sample_rate = sample_rate
        self.channels = channels
        self.application = application
        self.bitrate = None
        self.vbr = True
        self.encoded_frames: list[tuple[int, int]] = []
        type(self).instances.append(self)

    def encode(self, pcm: bytes, samples: int) -> bytes:
        self.encoded_frames.append((len(pcm), samples))
        # Deterministic stand-in payload tagged with frame ordinal.
        idx = len(self.encoded_frames)
        return b"\xfa" + idx.to_bytes(2, "big") + b"opus"


class _StubOpuslib:
    """Minimal `opuslib` stub good enough for the tts module's needs."""

    APPLICATION_VOIP = 2048

    Encoder = _StubOpusEncoder


@pytest.fixture(autouse=True)
def _stub_opuslib(monkeypatch):
    """Inject a fake opuslib into sys.modules so tts can lazy-import it."""
    import sys
    _StubOpusEncoder.instances = []
    monkeypatch.setitem(sys.modules, "opuslib", _StubOpuslib)
    yield


# Fake httpx.AsyncClient with .stream(...) for the TTS HTTP boundary.
class _FakeStreamResponse:
    def __init__(self, chunks: list[bytes], status: int = 200):
        self._chunks = chunks
        self.status_code = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


class _FakeAsyncClient:
    """Captures the POST + replays a canned PCM stream body."""

    last_call: dict[str, Any] | None = None
    pcm_chunks: list[bytes] = []
    status_code: int = 200

    def __init__(self, *_, **__):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def stream(self, method, url, *, headers, json):
        type(self).last_call = {
            "method": method,
            "url": url,
            "headers": headers,
            "json": json,
        }
        return _FakeStreamResponse(type(self).pcm_chunks, status=type(self).status_code)


@pytest.fixture
def fake_openai(monkeypatch):
    """Wire the fake httpx client + a TTS API key for the duration of one test."""
    _FakeAsyncClient.last_call = None
    _FakeAsyncClient.pcm_chunks = []
    _FakeAsyncClient.status_code = 200
    monkeypatch.setattr(tts, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(tts.httpx, "AsyncClient", _FakeAsyncClient)
    yield _FakeAsyncClient


# ---------------------------------------------------------------------------
# 1. OpenAI request shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_posts_correct_openai_shape(fake_openai):
    """tts-1 POST carries model, input, voice, response_format=pcm."""
    # One chunk of 24 kHz mono int16 PCM, 100 ms long, all silence.
    pcm_24k_100ms = b"\x00\x00" * (24000 * 100 // 1000)
    fake_openai.pcm_chunks = [pcm_24k_100ms]

    frames = []
    async for f in tts.synthesize_to_opus_frames("hello there"):
        frames.append(f)

    call = fake_openai.last_call
    assert call is not None
    assert call["method"] == "POST"
    assert call["url"] == tts.OPENAI_TTS_URL
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["json"]["model"] == "tts-1"
    assert call["json"]["voice"] == "alloy"
    assert call["json"]["input"] == "hello there"
    assert call["json"]["response_format"] == "pcm"


@pytest.mark.asyncio
async def test_synthesize_voice_and_model_overrides_apply(fake_openai):
    """Per-call voice/model kwargs override the env defaults."""
    fake_openai.pcm_chunks = [b"\x00\x00" * (24000 * 40 // 1000)]
    async for _ in tts.synthesize_to_opus_frames(
        "test", voice="onyx", model="tts-1-hd"
    ):
        pass
    assert fake_openai.last_call["json"]["voice"] == "onyx"
    assert fake_openai.last_call["json"]["model"] == "tts-1-hd"


@pytest.mark.asyncio
async def test_synthesize_empty_input_yields_nothing(fake_openai):
    frames = [f async for f in tts.synthesize_to_opus_frames("")]
    assert frames == []
    # And for whitespace.
    frames = [f async for f in tts.synthesize_to_opus_frames("   \n\t")]
    assert frames == []
    # No HTTP call should have been made.
    assert fake_openai.last_call is None


@pytest.mark.asyncio
async def test_synthesize_no_key_yields_nothing(monkeypatch):
    monkeypatch.setattr(tts, "OPENAI_API_KEY", "")
    frames = [f async for f in tts.synthesize_to_opus_frames("hello")]
    assert frames == []


# ---------------------------------------------------------------------------
# 2. WS message ordering: audio_start -> binary frames -> audio_end
# ---------------------------------------------------------------------------


def test_drain_emits_audio_start_frames_audio_end_in_order(client, monkeypatch, fake_openai):
    """Full pipeline: STT mock -> reply -> TTS streams audio_start/binary/audio_end."""
    async def fake_transcribe(_pcm: bytes) -> str:
        return "what time is it?"

    async def fake_post_to_soul(_t: str, *, session_id: str | None = None) -> str:
        return "Half past nine, sir."

    monkeypatch.setattr(stt, "decode_opus_buffer", lambda frames: b"\x00" * 16000)
    monkeypatch.setattr(stt, "segment_on_vad", lambda pcm, sr: [pcm])
    monkeypatch.setattr(stt, "transcribe", fake_transcribe)
    monkeypatch.setattr(stt, "post_to_soul", fake_post_to_soul)

    # 200 ms of 24 kHz mono PCM split across 2 chunks so we exercise the
    # multi-chunk resampler path.
    half = b"\x00\x00" * (24000 * 100 // 1000)
    fake_openai.pcm_chunks = [half, half]

    with _connect(client) as ws:
        json.loads(ws.receive_text())  # hello
        ws.send_bytes(b"opus-payload")
        ws.send_text(json.dumps({"type": "end_utterance", "seq": 11}))

        ack = json.loads(ws.receive_text())
        transcript = json.loads(ws.receive_text())
        assistant = json.loads(ws.receive_text())
        audio_start = json.loads(ws.receive_text())

        # Drain binary frames until we see audio_end as the next text frame.
        binary_frames: list[bytes] = []
        audio_end = None
        while True:
            msg = ws.receive()
            if "bytes" in msg and msg["bytes"] is not None:
                binary_frames.append(msg["bytes"])
                continue
            if "text" in msg and msg["text"] is not None:
                audio_end = json.loads(msg["text"])
                break
            # Anything else is unexpected; bail out.
            pytest.fail(f"unexpected ws message kind: {msg!r}")

    # Order: ack -> transcript -> assistant_text -> audio_start -> [binary]* -> audio_end
    assert ack["type"] == "ack"
    assert transcript["type"] == "transcript"
    assert assistant["type"] == "assistant_text"
    assert assistant["text"] == "Half past nine, sir."

    assert audio_start["type"] == "audio_start"
    assert audio_start["format"] == "opus"
    assert audio_start["sample_rate"] == 16000
    assert audio_start["channels"] == 1
    assert audio_start["frame_ms"] == 20

    assert audio_end is not None
    assert audio_end["type"] == "audio_end"
    assert audio_end["seq"] == audio_start["seq"]
    assert audio_end["frames"] == len(binary_frames)
    assert audio_end["bytes"] == sum(len(b) for b in binary_frames)
    assert len(binary_frames) > 0  # we did stream something

    # Stub encoder produces a deterministic prefix; verify all frames look like opus blobs.
    for f in binary_frames:
        assert f.startswith(b"\xfa")

    # Session counters reflect the synthesis.
    session = list(SESSIONS.values())[0]
    assert session.tts_seq == audio_start["seq"]
    assert session.tts_frames_out == len(binary_frames)
    assert session.tts_bytes_out == sum(len(b) for b in binary_frames)
    assert isinstance(session.tts_synth_latency_ms, int)


def test_audio_start_seq_increments_per_utterance(client, monkeypatch, fake_openai):
    """Two consecutive drains yield audio_start.seq=1 then audio_start.seq=2."""
    counter = {"n": 0}

    async def fake_transcribe(_pcm: bytes) -> str:
        counter["n"] += 1
        return f"utterance {counter['n']}"

    async def fake_post_to_soul(_t: str, *, session_id: str | None = None) -> str:
        return "ack"

    monkeypatch.setattr(stt, "decode_opus_buffer", lambda frames: b"\x00" * 16000)
    monkeypatch.setattr(stt, "segment_on_vad", lambda pcm, sr: [pcm])
    monkeypatch.setattr(stt, "transcribe", fake_transcribe)
    monkeypatch.setattr(stt, "post_to_soul", fake_post_to_soul)

    fake_openai.pcm_chunks = [b"\x00\x00" * (24000 * 60 // 1000)]

    seqs: list[int] = []
    with _connect(client) as ws:
        json.loads(ws.receive_text())  # hello
        for n in (1, 2):
            ws.send_bytes(b"opus-payload")
            ws.send_text(json.dumps({"type": "end_utterance", "seq": n}))
            json.loads(ws.receive_text())  # ack
            json.loads(ws.receive_text())  # transcript
            json.loads(ws.receive_text())  # assistant_text
            audio_start = json.loads(ws.receive_text())
            seqs.append(audio_start["seq"])
            # Drain binary + audio_end so the next iteration starts clean.
            while True:
                m = ws.receive()
                if "text" in m and m["text"] is not None:
                    break

    assert seqs == [1, 2]


# ---------------------------------------------------------------------------
# 3. Frame size = 20 ms (640 bytes pre-encode)
# ---------------------------------------------------------------------------


def test_pcm_to_opus_frames_pure_helper_carves_20ms_frames():
    """Pure helper: 5 frames worth of PCM -> 5 Opus packets, each fed 320 samples."""
    n_frames = 5
    pcm = b"\x00\x00" * (320 * n_frames)  # 5 * 20 ms at 16 kHz mono int16
    out = list(tts._pcm_to_opus_frames(pcm))
    assert len(out) == n_frames
    enc = _StubOpusEncoder.instances[-1]
    # Every encode call received exactly 640 bytes of PCM (320 samples).
    for byte_len, sample_count in enc.encoded_frames:
        assert byte_len == 640
        assert sample_count == 320


def test_pcm_to_opus_frames_drops_partial_trailing_frame():
    """Trailing PCM shorter than one frame is dropped silently."""
    pcm = b"\x00\x00" * (320 * 2 + 100)  # 2 full frames + remainder
    out = list(tts._pcm_to_opus_frames(pcm))
    assert len(out) == 2


def test_pcm_to_opus_frames_empty_input_yields_nothing():
    assert list(tts._pcm_to_opus_frames(b"")) == []


def test_pcm_to_opus_frames_constants_match_codec_spec():
    """Sanity: 16 kHz * 20 ms = 320 samples, 320 * 2 bytes = 640."""
    assert tts.SAMPLES_PER_FRAME == 320
    assert tts.BYTES_PER_FRAME == 640
    assert tts.OUTPUT_SAMPLE_RATE == 16000
    assert tts.OUTPUT_CHANNELS == 1
    assert tts.OUTPUT_FRAME_MS == 20
    assert tts.OUTPUT_BITRATE_BPS == 32000


# ---------------------------------------------------------------------------
# 4. Error path emits error frame, does NOT crash the WS
# ---------------------------------------------------------------------------


def test_tts_error_emits_error_frame_and_audio_end(client, monkeypatch, fake_openai):
    """OpenAI 5xx surfaces as `error` with stage="tts"; WS stays usable."""
    async def fake_transcribe(_pcm: bytes) -> str:
        return "trigger tts"

    async def fake_post_to_soul(_t: str, *, session_id: str | None = None) -> str:
        return "Reply that fails to synthesize."

    monkeypatch.setattr(stt, "decode_opus_buffer", lambda frames: b"\x00" * 16000)
    monkeypatch.setattr(stt, "segment_on_vad", lambda pcm, sr: [pcm])
    monkeypatch.setattr(stt, "transcribe", fake_transcribe)
    monkeypatch.setattr(stt, "post_to_soul", fake_post_to_soul)

    # 503 from OpenAI mid-stream.
    fake_openai.pcm_chunks = []
    fake_openai.status_code = 503

    with _connect(client) as ws:
        json.loads(ws.receive_text())  # hello
        ws.send_bytes(b"opus-payload")
        ws.send_text(json.dumps({"type": "end_utterance", "seq": 99}))

        json.loads(ws.receive_text())  # ack
        json.loads(ws.receive_text())  # transcript
        json.loads(ws.receive_text())  # assistant_text
        audio_start = json.loads(ws.receive_text())
        # No binary frames (HTTP 503 raises before any chunk) — next is
        # audio_end (frames=0), then the error envelope.
        audio_end = json.loads(ws.receive_text())
        err = json.loads(ws.receive_text())

        assert audio_start["type"] == "audio_start"
        assert audio_end["type"] == "audio_end"
        assert audio_end["frames"] == 0
        assert audio_end["bytes"] == 0
        assert err["type"] == "error"
        assert err["stage"] == "tts"
        assert err["reason"] == "tts_synth_failed"
        assert "503" in err["detail"] or "HTTP" in err["detail"]
        assert err["seq"] == audio_start["seq"]

        # WS still usable — round-trip a noop control message.
        ws.send_text(json.dumps({"type": "noop", "seq": 100}))
        ack2 = json.loads(ws.receive_text())
        assert ack2["type"] == "ack" and ack2["echo_type"] == "noop"


def test_tts_no_api_key_emits_empty_audio_envelope(client, monkeypatch):
    """Missing OPENAI_API_KEY: audio_start + audio_end (frames=0), no error."""
    async def fake_transcribe(_pcm: bytes) -> str:
        return "hello"

    async def fake_post_to_soul(_t: str, *, session_id: str | None = None) -> str:
        return "Hi back."

    monkeypatch.setattr(stt, "decode_opus_buffer", lambda frames: b"\x00" * 16000)
    monkeypatch.setattr(stt, "segment_on_vad", lambda pcm, sr: [pcm])
    monkeypatch.setattr(stt, "transcribe", fake_transcribe)
    monkeypatch.setattr(stt, "post_to_soul", fake_post_to_soul)
    monkeypatch.setattr(tts, "OPENAI_API_KEY", "")

    with _connect(client) as ws:
        json.loads(ws.receive_text())  # hello
        ws.send_bytes(b"opus-payload")
        ws.send_text(json.dumps({"type": "end_utterance", "seq": 1}))

        json.loads(ws.receive_text())  # ack
        json.loads(ws.receive_text())  # transcript
        json.loads(ws.receive_text())  # assistant_text
        audio_start = json.loads(ws.receive_text())
        audio_end = json.loads(ws.receive_text())

        assert audio_start["type"] == "audio_start"
        assert audio_end["type"] == "audio_end"
        assert audio_end["frames"] == 0

        # Round-trip a control message to confirm the receive loop is alive.
        ws.send_text(json.dumps({"type": "noop", "seq": 2}))
        ack = json.loads(ws.receive_text())
        assert ack["type"] == "ack"


# ---------------------------------------------------------------------------
# Imports clean + pyproject [voice] extra unchanged
# ---------------------------------------------------------------------------


def test_module_imports_clean():
    """`from alfred_coo.fleet_voice import server, stt, tts` must succeed."""
    from alfred_coo.fleet_voice import server, stt, tts  # noqa: F401
    # And the public surface is what server.py reaches for.
    assert callable(tts.synthesize_to_opus_frames)
    assert callable(tts._pcm_to_opus_frames)


def test_pyproject_voice_extra_unchanged():
    """SAL-4005 must not silently expand the [voice] extra; openai/webrtcvad/opuslib only."""
    here = Path(__file__).resolve().parent.parent
    with open(here / "pyproject.toml", "rb") as f:
        cfg = tomllib.load(f)
    voice_deps = cfg["project"]["optional-dependencies"]["voice"]
    # Strip version specifiers to compare names.
    names = sorted(d.split(">=")[0].split("==")[0].split("<")[0].split("~")[0].strip()
                   for d in voice_deps)
    assert names == sorted(["openai", "webrtcvad-wheels", "opuslib"]), (
        f"unexpected [voice] extra contents: {voice_deps}"
    )
