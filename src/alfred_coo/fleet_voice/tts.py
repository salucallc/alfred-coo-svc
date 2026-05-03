"""OpenAI TTS adapter for the fleet_voice gateway (SAL-4005, P2.1).

Plumbs Alfred's text reply (already returned by `stt.post_to_soul`) through:

    text  --tts-1-->  PCM (24 kHz mono int16)
          --resample-> PCM (16 kHz mono int16)
          --opus----> 20 ms Opus frames at 32 kbps CBR
          --yield---> binary frames pushed down the WebSocket

The encoded frames are yielded as `bytes` from `synthesize_to_opus_frames` so
the WS sender in `server.py` can `await session.send_binary(frame)` per frame
and let TCP-level back-pressure naturally slow OpenAI's HTTP body when the
device's Wi-Fi link is congested.

Design notes (mirrors the SAL-4003 stt.py shape):

  * `synthesize_to_opus_frames` is `async` and yields chunks; the underlying
    OpenAI HTTP request uses `httpx.AsyncClient.stream(...)` with
    `response_format="pcm"`. PCM is OpenAI's lowest-latency format
    (no MP3 decode roundtrip; container-free 24 kHz mono int16).

  * `_pcm_to_opus_frames` is a pure synchronous generator over a PCM blob.
    Easy to unit-test against a known-size PCM buffer; the encoder is held
    inside the generator so each `synthesize_to_opus_frames` invocation
    gets a fresh `opuslib.Encoder` (Opus encoders carry state).

  * `opuslib` is lazy-imported (same reason as stt.py — keeps the module
    importable on dev boxes without `libopus.dll` on PATH; tests mock the
    import boundary).

  * Resampling 24 kHz -> 16 kHz uses stdlib `audioop.ratecv` so we avoid
    pulling `pyav` for what is a single-line stateful resampler. This
    matches the pattern alfred-voice/main.py uses for the inbound side
    of its TTS pipeline.

  * Codec params per PLAN.md decision #11: 16 kHz mono, 20 ms frames,
    32 kbps CBR. Renegotiation to 48 kHz stereo 96 kbps for music happens
    in P3 (SAL-4015 audio_pipe), not here.

  * Errors propagate to the caller as a single exception. `server.py`
    catches and emits an `error` frame with `stage:"tts"` so the WS stays
    open on TTS failure (mirrors STT drain failure handling).
"""

from __future__ import annotations

import audioop
import logging
import os
import time
from typing import AsyncIterator, Iterator

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Output codec (matches inbound voice path; music renegotiation in P3)
# ---------------------------------------------------------------------------

OUTPUT_SAMPLE_RATE = int(os.getenv("FLEET_VOICE_TTS_SAMPLE_RATE", "16000"))
"""Voice downlink rate. 16 kHz mono per PLAN.md decision #11."""

OUTPUT_CHANNELS = 1
"""Mono. Speaker is single-driver; stereo is music-path territory."""

OUTPUT_FRAME_MS = int(os.getenv("FLEET_VOICE_TTS_FRAME_MS", "20"))
"""20 ms per Opus frame matches the device decoder's jitter budget (60 ms)."""

OUTPUT_BITRATE_BPS = int(os.getenv("FLEET_VOICE_TTS_BITRATE_BPS", "32000"))
"""32 kbps CBR. Plenty of headroom for speech at 16 kHz mono."""

# ---------------------------------------------------------------------------
# OpenAI TTS config
# ---------------------------------------------------------------------------

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
"""Same key as STT; OpenAI's audio.speech endpoint reuses it."""

OPENAI_TTS_URL = os.getenv(
    "OPENAI_TTS_URL", "https://api.openai.com/v1/audio/speech"
)
"""Override only for tests / regional endpoints."""

TTS_MODEL = os.getenv("FLEET_VOICE_TTS_MODEL", "tts-1")
"""tts-1 (lower latency) per plan decision #2; tts-1-hd is a future swap."""

TTS_VOICE = os.getenv("FLEET_VOICE_TTS_VOICE", "alloy")
"""Default voice; alfred-voice/main.py uses 'fable', but 'alloy' is the OpenAI
default and feels less era-flavoured. Override per-call via the function arg."""

TTS_RESPONSE_FORMAT = os.getenv("FLEET_VOICE_TTS_RESPONSE_FORMAT", "pcm")
"""`pcm` returns container-free 24 kHz mono int16 LE; lowest decode latency.
Alternatives (`mp3`, `wav`, `opus`) require a separate decode step."""

TTS_INPUT_SAMPLE_RATE = int(os.getenv("FLEET_VOICE_TTS_INPUT_SAMPLE_RATE", "24000"))
"""tts-1 native PCM rate. Documented as 24 kHz mono int16 little-endian."""

HTTP_TIMEOUT_S = float(os.getenv("FLEET_VOICE_TTS_HTTP_TIMEOUT_S", "30"))

# ---------------------------------------------------------------------------
# ElevenLabs TTS config (alternative backend)
# ---------------------------------------------------------------------------

TTS_BACKEND = os.getenv("FLEET_VOICE_TTS_BACKEND", "openai").lower()
"""Which TTS service to call. Values: ``openai`` (default), ``elevenlabs``.
Switch to ``elevenlabs`` when the ``OPENAI_API_KEY`` is OpenRouter (no
``/v1/audio/speech`` proxy) or when better voice quality is wanted."""

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")

ELEVENLABS_VOICE_ID = os.getenv(
    "FLEET_VOICE_ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb"
)
"""Default to George (British male) — Alfred-appropriate.
Other stock voices: 21m00Tcm4TlvDq8ikWAM (Rachel), TxGEqnHWrfWFTfGW9XjX (Josh)."""

ELEVENLABS_MODEL = os.getenv(
    "FLEET_VOICE_ELEVENLABS_MODEL", "eleven_turbo_v2_5"
)
"""eleven_turbo_v2_5 (fast, multilingual). eleven_flash_v2_5 = fastest, slightly
lower quality. eleven_multilingual_v2 = highest quality, slower."""

ELEVENLABS_TTS_URL = (
    "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
    "?output_format=pcm_16000"
)
"""ElevenLabs streaming endpoint. ``pcm_16000`` = 16 kHz mono int16 LE,
matches OUTPUT_SAMPLE_RATE so no resample needed."""

# Opus frame bookkeeping derived from the codec params above.
SAMPLES_PER_FRAME = OUTPUT_SAMPLE_RATE * OUTPUT_FRAME_MS // 1000
"""Samples per 20 ms Opus frame. At 16 kHz that's 320."""

BYTES_PER_FRAME = SAMPLES_PER_FRAME * 2 * OUTPUT_CHANNELS
"""Pre-encode PCM bytes per Opus frame. At 16 kHz mono int16 that's 640."""


# ---------------------------------------------------------------------------
# Pure helper: PCM -> Opus frames
# ---------------------------------------------------------------------------


def _pcm_to_opus_frames(pcm_16khz_mono: bytes) -> Iterator[bytes]:
    """Yield 20 ms Opus frames from a contiguous 16 kHz mono int16 PCM blob.

    The encoder is created per-call (Opus encoders carry state between
    frames; sharing one across synthesis calls would corrupt the stream).
    Trailing PCM that is shorter than one full frame is dropped silently;
    the caller is expected to feed whole frames worth or accept the cut.

    Returns an iterator of `bytes`, one Opus packet per yield. Empty input
    yields nothing so the caller can no-op without exception handling.

    Raises `RuntimeError` if `opuslib` is unavailable. Same lazy-import
    pattern as `stt.decode_opus_buffer` so the module loads on dev boxes.
    """
    if not pcm_16khz_mono:
        return
    try:
        import av  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - exercised only on bare boxes
        raise RuntimeError(
            "pyav/numpy unavailable for TTS opus encode. "
            "Original error: {}".format(exc)
        ) from exc

    # PyAV/libopus encoder. Bundles libopus on Windows via the pyav wheel,
    # so no separate libopus.dll install is needed (the historical pain
    # point for opuslib on this dev box). Same pattern STT uses for decode.
    codec = av.CodecContext.create("opus", "w")
    codec.sample_rate = OUTPUT_SAMPLE_RATE
    codec.layout = "mono" if OUTPUT_CHANNELS == 1 else "stereo"
    codec.format = "s16"
    codec.bit_rate = OUTPUT_BITRATE_BPS
    try:
        codec.options = {"vbr": "off", "application": "voip"}
    except Exception:
        pass

    bytes_per_frame = BYTES_PER_FRAME
    samples_per_frame = SAMPLES_PER_FRAME
    total = len(pcm_16khz_mono)
    pts = 0
    for i in range(0, total - bytes_per_frame + 1, bytes_per_frame):
        chunk = pcm_16khz_mono[i:i + bytes_per_frame]
        samples = np.frombuffer(chunk, dtype=np.int16).copy()
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1),
                                           format="s16",
                                           layout=codec.layout.name)
        frame.sample_rate = OUTPUT_SAMPLE_RATE
        frame.pts = pts
        pts += samples_per_frame
        try:
            for packet in codec.encode(frame):
                pkt_bytes = bytes(packet)
                if pkt_bytes:
                    yield pkt_bytes
        except Exception as exc:
            logger.warning(
                "fleet_voice TTS opus encode failed on frame "
                "offset=%d bytes=%d: %s", i, len(chunk), exc
            )
            continue

    # Flush any held samples in the encoder buffer.
    try:
        for packet in codec.encode(None):
            pkt_bytes = bytes(packet)
            if pkt_bytes:
                yield pkt_bytes
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Resampling helper
# ---------------------------------------------------------------------------


def _resample_to_16k_mono(pcm: bytes, src_rate: int, state: object | None) -> tuple[bytes, object | None]:
    """Resample int16 mono PCM from `src_rate` to 16 kHz using audioop.ratecv.

    Returns the resampled bytes plus the carry-state so the next chunk can
    pick up without phase discontinuity. `state=None` on the first call.

    If `src_rate == 16000` we pass through unchanged with no state.
    """
    if not pcm:
        return b"", state
    if src_rate == OUTPUT_SAMPLE_RATE:
        return pcm, state
    out, new_state = audioop.ratecv(pcm, 2, 1, src_rate, OUTPUT_SAMPLE_RATE, state)
    return out, new_state


# ---------------------------------------------------------------------------
# OpenAI TTS streaming
# ---------------------------------------------------------------------------


async def synthesize_to_opus_frames(
    text: str,
    *,
    voice: str | None = None,
    model: str | None = None,
) -> AsyncIterator[bytes]:
    """Backend-agnostic entry. Routes to OpenAI or ElevenLabs based on
    ``FLEET_VOICE_TTS_BACKEND`` env var."""
    if not text or not text.strip():
        return
    backend = TTS_BACKEND
    if backend == "elevenlabs":
        async for pkt in _synthesize_elevenlabs_to_opus_frames(text, voice=voice, model=model):
            yield pkt
        return
    async for pkt in _synthesize_openai_to_opus_frames(text, voice=voice, model=model):
        yield pkt


async def _synthesize_openai_to_opus_frames(
    text: str,
    *,
    voice: str | None = None,
    model: str | None = None,
) -> AsyncIterator[bytes]:
    """Stream `text` through OpenAI tts-1 and yield 16 kHz mono Opus frames.

    Pipeline:
      1. POST to OpenAI `/v1/audio/speech` with `response_format=pcm` and
         streaming body (`httpx.AsyncClient.stream`). Body is raw 24 kHz
         mono int16 LE PCM.
      2. Each HTTP body chunk is resampled 24 -> 16 kHz with carry-state.
      3. Resampled PCM is buffered until at least one 20 ms frame is
         available, then `_pcm_to_opus_frames` encodes whole frames out
         of the buffer; partial-frame remainder is held for the next
         chunk so we do not introduce a click between chunks.
      4. Each Opus packet is yielded as it lands; the WS sender awaits
         on `send_bytes`, giving us natural back-pressure when the
         device cannot keep up.

    Empty / whitespace-only input yields nothing. Missing API key yields
    nothing and logs a warning (same survivable-degraded mode as STT).

    Errors during streaming propagate as the original exception so the
    caller in `server.py` can emit `{"type":"error","stage":"tts",...}`
    and keep the WS open.
    """
    if not text or not text.strip():
        return
    if not OPENAI_API_KEY:
        # Survivable degraded: log and emit nothing. Caller's audio_start
        # was already sent; we will follow up with audio_end frames=0.
        logger.warning(
            "fleet_voice TTS: OPENAI_API_KEY unset; skipping synthesis"
        )
        return

    payload = {
        "model": model or TTS_MODEL,
        "input": text,
        "voice": voice or TTS_VOICE,
        "response_format": TTS_RESPONSE_FORMAT,
    }
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    # Re-encode state across chunk boundaries.
    resample_state: object | None = None
    pcm_carry = b""
    # Hand-roll a single pyav opus encoder here so the same encoder state
    # spans all chunks (avoids boundary clicks). Same lib choice as
    # _pcm_to_opus_frames -- pyav bundles libopus on Windows.
    try:
        import av  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - bare box only
        raise RuntimeError(
            "pyav/numpy unavailable for TTS opus encode. Original: {}".format(exc)
        ) from exc

    codec = av.CodecContext.create("opus", "w")
    codec.sample_rate = OUTPUT_SAMPLE_RATE
    codec.layout = "mono" if OUTPUT_CHANNELS == 1 else "stereo"
    codec.format = "s16"
    codec.bit_rate = OUTPUT_BITRATE_BPS
    try:
        codec.options = {"vbr": "off", "application": "voip"}
    except Exception:
        pass

    bytes_per_frame = BYTES_PER_FRAME
    samples_per_frame = SAMPLES_PER_FRAME
    pts = 0
    layout_name = codec.layout.name

    started = time.monotonic()
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        async with client.stream(
            "POST", OPENAI_TTS_URL, headers=headers, json=payload
        ) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                if not chunk:
                    continue
                resampled, resample_state = _resample_to_16k_mono(
                    chunk, TTS_INPUT_SAMPLE_RATE, resample_state
                )
                pcm_carry += resampled
                # Slice off whole frames; keep the partial-frame remainder
                # in pcm_carry for the next iteration.
                while len(pcm_carry) >= bytes_per_frame:
                    frame_pcm = pcm_carry[:bytes_per_frame]
                    pcm_carry = pcm_carry[bytes_per_frame:]
                    samples = np.frombuffer(frame_pcm, dtype=np.int16).copy()
                    frame = av.AudioFrame.from_ndarray(
                        samples.reshape(1, -1), format="s16", layout=layout_name
                    )
                    frame.sample_rate = OUTPUT_SAMPLE_RATE
                    frame.pts = pts
                    pts += samples_per_frame
                    try:
                        for packet in codec.encode(frame):
                            pkt_bytes = bytes(packet)
                            if pkt_bytes:
                                yield pkt_bytes
                    except Exception as exc:
                        logger.warning(
                            "fleet_voice TTS encode failure mid-stream: %s", exc
                        )
                        continue
    # Flush trailing partial frame: pad with zeros to one full frame so we
    # don't truncate the last syllable's tail.
    if pcm_carry:
        padded = pcm_carry + (b"\x00" * (bytes_per_frame - len(pcm_carry)))
        samples = np.frombuffer(padded, dtype=np.int16).copy()
        frame = av.AudioFrame.from_ndarray(
            samples.reshape(1, -1), format="s16", layout=layout_name
        )
        frame.sample_rate = OUTPUT_SAMPLE_RATE
        frame.pts = pts
        try:
            for packet in codec.encode(frame):
                pkt_bytes = bytes(packet)
                if pkt_bytes:
                    yield pkt_bytes
        except Exception as exc:
            logger.warning("fleet_voice TTS final-frame encode failure: %s", exc)
    # Drain encoder.
    try:
        for packet in codec.encode(None):
            pkt_bytes = bytes(packet)
            if pkt_bytes:
                yield pkt_bytes
    except Exception:
        pass
    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "fleet_voice TTS synthesis complete chars=%d elapsed_ms=%d",
        len(text), elapsed_ms,
    )


# ---------------------------------------------------------------------------
# ElevenLabs TTS streaming
# ---------------------------------------------------------------------------


async def _synthesize_elevenlabs_to_opus_frames(
    text: str,
    *,
    voice: str | None = None,
    model: str | None = None,
) -> AsyncIterator[bytes]:
    """Stream ``text`` through ElevenLabs and yield 16 kHz mono Opus frames.

    Uses the ``pcm_16000`` output format (mono int16 LE 16 kHz) which matches
    OUTPUT_SAMPLE_RATE — no resample needed (compare OpenAI which delivers
    24 kHz and forces a downsample). Same pyav opus encoder pattern as the
    OpenAI path so the wire shape is identical at the device end.
    """
    if not ELEVENLABS_API_KEY:
        logger.warning(
            "fleet_voice TTS: ELEVENLABS_API_KEY unset; skipping synthesis"
        )
        return

    voice_id = voice or ELEVENLABS_VOICE_ID
    payload = {
        "text": text,
        "model_id": model or ELEVENLABS_MODEL,
    }
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/pcm",
    }
    url = ELEVENLABS_TTS_URL.format(voice_id=voice_id)

    try:
        import av  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except Exception as exc:
        raise RuntimeError(
            "pyav/numpy unavailable for TTS opus encode. Original: {}".format(exc)
        ) from exc

    codec = av.CodecContext.create("opus", "w")
    codec.sample_rate = OUTPUT_SAMPLE_RATE
    codec.layout = "mono" if OUTPUT_CHANNELS == 1 else "stereo"
    codec.format = "s16"
    codec.bit_rate = OUTPUT_BITRATE_BPS
    try:
        codec.options = {"vbr": "off", "application": "voip"}
    except Exception:
        pass

    bytes_per_frame = BYTES_PER_FRAME
    samples_per_frame = SAMPLES_PER_FRAME
    pts = 0
    layout_name = codec.layout.name
    pcm_carry = b""

    started = time.monotonic()
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                if not chunk:
                    continue
                # ElevenLabs returns PCM directly at 16 kHz mono int16 LE.
                pcm_carry += chunk
                while len(pcm_carry) >= bytes_per_frame:
                    frame_pcm = pcm_carry[:bytes_per_frame]
                    pcm_carry = pcm_carry[bytes_per_frame:]
                    samples = np.frombuffer(frame_pcm, dtype=np.int16).copy()
                    frame = av.AudioFrame.from_ndarray(
                        samples.reshape(1, -1), format="s16", layout=layout_name
                    )
                    frame.sample_rate = OUTPUT_SAMPLE_RATE
                    frame.pts = pts
                    pts += samples_per_frame
                    try:
                        for packet in codec.encode(frame):
                            pkt_bytes = bytes(packet)
                            if pkt_bytes:
                                yield pkt_bytes
                    except Exception as exc:
                        logger.warning(
                            "fleet_voice TTS encode failure mid-stream: %s", exc
                        )
                        continue

    if pcm_carry:
        padded = pcm_carry + (b"\x00" * (bytes_per_frame - len(pcm_carry)))
        samples = np.frombuffer(padded, dtype=np.int16).copy()
        frame = av.AudioFrame.from_ndarray(
            samples.reshape(1, -1), format="s16", layout=layout_name
        )
        frame.sample_rate = OUTPUT_SAMPLE_RATE
        frame.pts = pts
        try:
            for packet in codec.encode(frame):
                pkt_bytes = bytes(packet)
                if pkt_bytes:
                    yield pkt_bytes
        except Exception as exc:
            logger.warning("fleet_voice TTS final-frame encode failure: %s", exc)
    try:
        for packet in codec.encode(None):
            pkt_bytes = bytes(packet)
            if pkt_bytes:
                yield pkt_bytes
    except Exception:
        pass
    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "fleet_voice TTS (elevenlabs) synthesis complete chars=%d elapsed_ms=%d "
        "voice=%s model=%s",
        len(text), elapsed_ms, voice_id, payload["model_id"],
    )
