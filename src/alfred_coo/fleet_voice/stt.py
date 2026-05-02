"""Whisper STT adapter for the fleet_voice gateway (SAL-4003, P1.5).

Plumbs the binary Opus payload that arrives on the WebSocket through:

    Opus frames  --decode-->  PCM (16 kHz mono int16)
                 --VAD-->     speech segments (silence-bounded utterances)
                 --Whisper--> transcript text

The transcript then flows two ways:

  1. Back down the WebSocket as a `transcript` JSON control frame so the
     device can light up its "Alfred heard you" UI cue. SAL-4006 will use
     this to feed an on-device debug log.
  2. To the soul-svc inference plane (`POST /v1/overview/chat`, see
     `alfred_coo.soul.SoulClient`-equivalent direct httpx call below).
     The chat reply is then echoed to the device as an `assistant_text`
     frame; TTS encoding lives in SAL-4005, so for now this is plain
     text the device can render or queue.

Design notes:

  * `decode_opus_buffer` and `transcribe` are both pure functions over
    bytes so unit tests can mock the I/O boundary cleanly. The opuslib
    and openai imports are deferred to call time so the module imports
    fine on machines without the native libopus DLL (Windows dev box,
    CI containers without `apt-get install libopus0`). The actual Pi /
    Linux runtime is fine, and tests mock both layers.

  * VAD strategy mirrors the proven path from `alfred-voice/main.py`
    `vad_trim`: WebRTC VAD with 20 ms frames at 16 kHz mono int16. We
    use aggressiveness level 2 (the alfred-voice value tested in the
    field) and a silence-bounded segmenter that emits one segment per
    contiguous speech run. Segments shorter than `MIN_SEGMENT_MS` are
    swallowed (keystroke / cough rejection); silence gaps shorter than
    `MAX_GAP_MS` inside a segment are bridged so a natural "uh, what
    I meant was ..." pause doesn't fragment one utterance into two
    Whisper calls.

  * Why not reuse XVF3800-side VAD? It's there (decision #3 in the
    plan), but the wire format right now is opaque Opus. A later ticket
    (SAL-4012-ish, "VAD signal in control plane") will let the device
    say "this Opus blob ends an utterance, transcribe now" and the
    server can skip its own VAD pass. Until then, server-side VAD is
    the keep-it-simple call.

  * OpenAI key: `OPENAI_API_KEY` env var (matches `alfred-voice/main.py`
    convention; see the README env table). Not hardcoded.

  * soul-svc endpoint: `/v1/overview/chat`. This is the chat-over-
    memory endpoint that the v2.0.0 soul-svc exposes per the close-
    ritual notes in the global CLAUDE.md. Sends the transcript as the
    user message; soul-svc returns a JSON body with a `response` (or
    `text`) field containing Alfred's reply.
"""

from __future__ import annotations

import logging
import os
import struct
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Audio config
# ---------------------------------------------------------------------------

SAMPLE_RATE = int(os.getenv("FLEET_VOICE_SAMPLE_RATE", "16000"))
"""Whisper-1 happily takes 16 kHz mono. Matches device uplink (decision #11)."""

CHANNELS = 1
"""Mono. Multi-mic mixing already happened on the XVF3800."""

FRAME_MS = int(os.getenv("FLEET_VOICE_FRAME_MS", "20"))
"""Opus + WebRTC VAD both like 10/20/30 ms; 20 ms is the device frame size."""

VAD_AGGRESSIVENESS = int(os.getenv("FLEET_VOICE_VAD_AGGRESSIVENESS", "2"))
"""0 (least aggressive / most permissive) .. 3 (cuts hardest). 2 = alfred-voice."""

MIN_SEGMENT_MS = int(os.getenv("FLEET_VOICE_MIN_SEGMENT_MS", "300"))
"""Drop sub-300 ms speech bursts (cough, key click, mic-bump)."""

MAX_GAP_MS = int(os.getenv("FLEET_VOICE_MAX_GAP_MS", "400"))
"""Bridge silence gaps <=400 ms inside one utterance (natural mid-sentence pause)."""

# ---------------------------------------------------------------------------
# Network config
# ---------------------------------------------------------------------------

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
"""Bearer for `https://api.openai.com/v1/audio/transcriptions` (Whisper-1)."""

OPENAI_STT_URL = os.getenv(
    "OPENAI_STT_URL", "https://api.openai.com/v1/audio/transcriptions"
)
"""Override only for tests / regional endpoints."""

WHISPER_MODEL = os.getenv("FLEET_VOICE_WHISPER_MODEL", "whisper-1")
"""Locked at whisper-1 per plan decision #12; Deepgram Nova later if needed."""

WHISPER_LANGUAGE = os.getenv("FLEET_VOICE_WHISPER_LANGUAGE", "en")
"""Forces English; remove env var for auto-detect."""

SOUL_API_URL = os.getenv(
    "SOUL_API_URL",
    # Default Oracle primary per the universal MCP failover rule. Tests
    # override this with a local URL via env or by mocking httpx.
    "http://100.105.27.63:8080",
)
SOUL_API_KEY = os.getenv("SOUL_API_KEY", "")
SOUL_CHAT_PATH = os.getenv("FLEET_VOICE_SOUL_CHAT_PATH", "/v1/overview/chat")
"""soul-svc v2.0.0 exposes chat-over-memory at `/v1/overview/chat`."""

SOUL_SESSION_ID = os.getenv("FLEET_VOICE_SOUL_SESSION_ID", "alfred-main")
"""Single shared session across all pucks until per-device sessions land."""

HTTP_TIMEOUT_S = float(os.getenv("FLEET_VOICE_HTTP_TIMEOUT_S", "30"))


# ---------------------------------------------------------------------------
# Opus decoding
# ---------------------------------------------------------------------------


def decode_opus_buffer(opus_frames: list[bytes]) -> bytes:
    """Decode a list of Opus frames to a single 16 kHz mono int16 PCM blob.

    Each entry in `opus_frames` is one Opus packet (one 20 ms frame).
    Returns little-endian int16 PCM concatenated in arrival order.

    Lazy-imports `opuslib` so the module loads on dev boxes that don't
    have native libopus available. If `opuslib` is missing or the DLL
    isn't loadable, raises `RuntimeError` with an actionable message.

    Empty input returns `b""` so the caller can short-circuit without
    a try/except.
    """
    if not opus_frames:
        return b""
    try:
        import opuslib  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - exercised only on bare boxes
        raise RuntimeError(
            "opuslib unavailable (install libopus on Linux: "
            "`apt-get install libopus0`; on Windows: opuslib needs "
            "libopus.dll on PATH). Original error: {}".format(exc)
        ) from exc

    decoder = opuslib.Decoder(SAMPLE_RATE, CHANNELS)
    frame_samples = SAMPLE_RATE * FRAME_MS // 1000
    out: list[bytes] = []
    for frame in opus_frames:
        if not frame:
            continue
        # decode() returns raw int16 little-endian PCM bytes for `frame_samples`.
        try:
            pcm = decoder.decode(frame, frame_samples)
        except Exception as exc:
            logger.warning("opus decode failed on %d-byte frame: %s", len(frame), exc)
            continue
        out.append(pcm)
    return b"".join(out)


# ---------------------------------------------------------------------------
# VAD segmentation
# ---------------------------------------------------------------------------


@dataclass
class _VadFrame:
    """One VAD-classified frame: raw PCM bytes + speech/silence verdict."""

    pcm: bytes
    is_speech: bool


def _slice_into_frames(pcm: bytes, sample_rate: int) -> list[bytes]:
    """Cut PCM into fixed-size FRAME_MS frames; drop the trailing partial."""
    bytes_per_sample = 2  # int16
    frame_bytes = sample_rate * FRAME_MS // 1000 * bytes_per_sample * CHANNELS
    if frame_bytes == 0:
        return []
    return [
        pcm[i:i + frame_bytes]
        for i in range(0, len(pcm) - frame_bytes + 1, frame_bytes)
    ]


def segment_on_vad(pcm: bytes, sample_rate: int) -> list[bytes]:
    """Split a PCM buffer into utterance-bounded PCM segments.

    The strategy mirrors the proven `alfred-voice/main.py` `vad_trim`
    flow but emits *multiple* segments (one per utterance) instead of a
    single trimmed blob. Output ordering matches input.

    Algorithm:
      1. Cut PCM into 20 ms frames.
      2. Run WebRTC VAD on each frame to label speech vs silence.
      3. Walk frames left-to-right, collecting consecutive speech frames
         into a current segment. A silence run shorter than `MAX_GAP_MS`
         is bridged into the current segment (so a natural mid-sentence
         pause doesn't cut us off). A silence run longer than that
         flushes the segment.
      4. Drop segments shorter than `MIN_SEGMENT_MS`.

    Lazy-imports `webrtcvad` for the same reason as opuslib above
    (keeps the module import-clean on bare boxes; tests patch this).
    """
    if not pcm:
        return []
    try:
        import webrtcvad  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - exercised only on bare boxes
        raise RuntimeError(
            "webrtcvad unavailable (pip install webrtcvad-wheels). "
            "Original error: {}".format(exc)
        ) from exc

    vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
    frames = _slice_into_frames(pcm, sample_rate)
    if not frames:
        return []

    classified: list[_VadFrame] = []
    for f in frames:
        try:
            speech = vad.is_speech(f, sample_rate)
        except Exception:
            # Length-mismatch frames at boundaries get classified as silence.
            speech = False
        classified.append(_VadFrame(f, speech))

    max_gap_frames = MAX_GAP_MS // FRAME_MS
    min_segment_frames = MIN_SEGMENT_MS // FRAME_MS

    segments: list[bytes] = []
    current: list[bytes] = []
    silence_run = 0  # consecutive silence frames inside the current segment

    for f in classified:
        if f.is_speech:
            current.append(f.pcm)
            silence_run = 0
            continue
        # silence frame
        if not current:
            # not in a segment yet — ignore leading silence
            continue
        silence_run += 1
        if silence_run <= max_gap_frames:
            # Treat brief silence as part of the same utterance.
            current.append(f.pcm)
            continue
        # Long silence flushes.
        # Trim the trailing silence we tentatively accumulated.
        speech_only = current[:-silence_run] if silence_run else current
        if len(speech_only) >= min_segment_frames:
            segments.append(b"".join(speech_only))
        current = []
        silence_run = 0

    # Flush the last segment if the buffer ended mid-speech.
    if current:
        # Strip trailing silence we may have bridged.
        while current and silence_run > 0:
            current.pop()
            silence_run -= 1
        if len(current) >= min_segment_frames:
            segments.append(b"".join(current))

    return segments


# ---------------------------------------------------------------------------
# Whisper transcription
# ---------------------------------------------------------------------------


def _pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw int16 mono PCM in a WAV container so Whisper accepts it.

    OpenAI's Whisper endpoint sniffs by container; raw PCM gets a 400.
    We hand-roll the 44-byte header instead of pulling in `wave` so the
    function stays pure (no temp files, no BytesIO ceremony).
    """
    if not pcm:
        return b""
    bits_per_sample = 16
    byte_rate = sample_rate * CHANNELS * bits_per_sample // 8
    block_align = CHANNELS * bits_per_sample // 8
    data_size = len(pcm)
    riff_size = 36 + data_size
    header = b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
    fmt = (
        b"fmt " + struct.pack(
            "<IHHIIHH",
            16,           # PCM fmt chunk size
            1,            # PCM format
            CHANNELS,
            sample_rate,
            byte_rate,
            block_align,
            bits_per_sample,
        )
    )
    data = b"data" + struct.pack("<I", data_size) + pcm
    return header + fmt + data


async def transcribe(pcm: bytes) -> str:
    """Send a PCM utterance to OpenAI Whisper-1 and return the transcript.

    Pure async for symmetry with the rest of fleet_voice (everything
    else awaits). Returns `""` on empty input or empty transcript so
    the caller can no-op without exception handling.

    Tests mock this whole function or the underlying httpx call.
    """
    if not pcm:
        return ""
    if not OPENAI_API_KEY:
        # We don't raise — empty transcript is a survivable degraded mode
        # (the gateway still records the audio frame, ops sees the gap).
        logger.warning(
            "fleet_voice STT: OPENAI_API_KEY unset; skipping transcription"
        )
        return ""
    wav = _pcm_to_wav(pcm, SAMPLE_RATE)
    files = {"file": ("utterance.wav", wav, "audio/wav")}
    data = {"model": WHISPER_MODEL}
    if WHISPER_LANGUAGE:
        data["language"] = WHISPER_LANGUAGE
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        resp = await client.post(OPENAI_STT_URL, headers=headers, files=files, data=data)
        resp.raise_for_status()
        body = resp.json()
    text = (body.get("text") or "").strip()
    logger.info("fleet_voice STT transcript: %r", text)
    return text


# ---------------------------------------------------------------------------
# soul-svc relay
# ---------------------------------------------------------------------------


async def post_to_soul(transcript: str, *, session_id: str | None = None) -> str:
    """Send a transcript to soul-svc `/v1/overview/chat` and return the reply.

    Returns the reply text (best-effort field extraction so we don't
    couple to one specific JSON shape; soul-svc's response field has
    moved between `response` and `text` between versions). Returns ``""``
    on missing key, transport error, or empty body — same survivable-
    degraded approach as `transcribe`.
    """
    if not transcript:
        return ""
    if not SOUL_API_KEY:
        logger.warning(
            "fleet_voice STT: SOUL_API_KEY unset; skipping soul-svc relay"
        )
        return ""
    url = f"{SOUL_API_URL.rstrip('/')}{SOUL_CHAT_PATH}"
    payload: dict[str, Any] = {
        "session_id": session_id or SOUL_SESSION_ID,
        "message": transcript,
    }
    headers = {
        "Authorization": f"Bearer {SOUL_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:
        logger.warning("fleet_voice STT: soul-svc relay failed: %s", exc)
        return ""
    if not isinstance(body, dict):
        return ""
    # Field-name tolerance: try the common shapes in priority order.
    for key in ("response", "text", "message", "reply", "content"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""
