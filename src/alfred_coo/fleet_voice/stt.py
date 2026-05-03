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
    # Use pyav (FFmpeg) for Opus decode; bundles libopus on Windows so we
    # don't need a separate libopus.dll install. The opuslib pure-Python
    # binding requires libopus.dll on PATH, which Windows dev boxes lack.
    try:
        import av
        import numpy as np
    except Exception as exc:  # pragma: no cover - exercised only on bare boxes
        raise RuntimeError(
            "pyav unavailable; pip install av (bundles ffmpeg + libopus). "
            "Original error: {}".format(exc)
        ) from exc

    # PyAV's Opus decoder always emits 48 kHz output regardless of the
    # encoder's internal rate (libopus decoder design). We use pyav's
    # AudioResampler (ffmpeg swresample) to land directly at SAMPLE_RATE
    # mono int16, so downstream sees a clean 16 kHz PCM blob with no
    # audioop intermediate. One library, one resampler.
    codec = av.CodecContext.create("opus", "r")
    codec.layout = "mono" if CHANNELS == 1 else "stereo"

    target_layout = "mono" if CHANNELS == 1 else "stereo"
    resampler = av.AudioResampler(
        format="s16",
        layout=target_layout,
        rate=SAMPLE_RATE,
    )

    out: list[bytes] = []
    detected_sr = None
    for frame in opus_frames:
        if not frame:
            continue
        try:
            packet = av.Packet(frame)
            for decoded in codec.decode(packet):
                if detected_sr is None:
                    detected_sr = decoded.sample_rate
                    if detected_sr != SAMPLE_RATE:
                        logger.info(
                            "fleet_voice STT: pyav decoded Opus at sr=%d, "
                            "resampling to %d via swresample",
                            detected_sr, SAMPLE_RATE,
                        )
                for resampled in resampler.resample(decoded):
                    arr = resampled.to_ndarray()
                    if arr.ndim > 1:
                        arr = arr[0] if arr.shape[0] == 1 else arr.mean(axis=0).astype(np.int16)
                    out.append(arr.tobytes())
        except Exception as exc:
            logger.warning("opus decode failed on %d-byte frame: %s", len(frame), exc)
            continue

    # Drain any samples held in the resampler buffer.
    try:
        for resampled in resampler.resample(None):
            arr = resampled.to_ndarray()
            if arr.ndim > 1:
                arr = arr[0] if arr.shape[0] == 1 else arr.mean(axis=0).astype(np.int16)
            out.append(arr.tobytes())
    except Exception:
        pass

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


# SAL-4006 smoke: env-selectable STT backend. Default is faster-whisper local
# because saluca-deploy/.env ships an OpenRouter key (no Whisper proxy).
# Set FLEET_VOICE_STT_BACKEND=openai to restore the cloud path once a real
# OpenAI key (or Groq via OPENAI_STT_URL override) is in env.
STT_BACKEND = os.getenv("FLEET_VOICE_STT_BACKEND", "faster_whisper").lower()
LOCAL_WHISPER_MODEL = os.getenv("FLEET_VOICE_LOCAL_MODEL", "base.en")
LOCAL_WHISPER_DEVICE = os.getenv("FLEET_VOICE_LOCAL_DEVICE", "cpu")
LOCAL_WHISPER_COMPUTE = os.getenv("FLEET_VOICE_LOCAL_COMPUTE", "int8")

_local_model = None


def _get_local_model():
    """Lazy-load the faster-whisper model on first use."""
    global _local_model
    if _local_model is None:
        from faster_whisper import WhisperModel
        logger.info(
            "fleet_voice STT: loading faster-whisper model=%s device=%s compute=%s",
            LOCAL_WHISPER_MODEL, LOCAL_WHISPER_DEVICE, LOCAL_WHISPER_COMPUTE,
        )
        _local_model = WhisperModel(
            LOCAL_WHISPER_MODEL,
            device=LOCAL_WHISPER_DEVICE,
            compute_type=LOCAL_WHISPER_COMPUTE,
        )
    return _local_model


async def transcribe(pcm: bytes) -> str:
    """Transcribe a PCM utterance.

    Backend selected by FLEET_VOICE_STT_BACKEND env (default `faster_whisper`).
    Returns `""` on empty input or empty transcript so the caller can no-op.
    """
    if not pcm:
        return ""

    if STT_BACKEND == "faster_whisper":
        import numpy as np
        import asyncio
        # int16 LE -> float32 in [-1, 1]
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

        # SAL-4006 band-aid: puck-side encoder appears to attenuate audio by
        # ~24 dB (suspect: right-shift 16 instead of 8 on 24-bit-in-32-bit
        # I2S samples). Until SAL-4001 firmware fix lands, normalize gateway-
        # side so Whisper sees a usable level. Target peak ~0.7 with a hard
        # cap so we don't amplify pure noise.
        peak_amp_pre = float(np.max(np.abs(audio))) if audio.size else 0.0
        gain_db_applied = 0.0
        if peak_amp_pre > 0.005 and peak_amp_pre < 0.7:
            target_peak = float(os.getenv("FLEET_VOICE_TARGET_PEAK", "0.7"))
            max_gain = float(os.getenv("FLEET_VOICE_MAX_GAIN", "32.0"))  # ~30 dB
            gain = min(target_peak / peak_amp_pre, max_gain)
            audio = np.clip(audio * gain, -1.0, 1.0)
            gain_db_applied = 20.0 * np.log10(gain) if gain > 0 else 0.0
            logger.info(
                "fleet_voice STT normalize: peak %.4f -> %.4f gain=%.1f dB",
                peak_amp_pre, float(np.max(np.abs(audio))), gain_db_applied,
            )

        loop = asyncio.get_event_loop()

        # SAL-4006 hallucination debug: dump the PCM Whisper sees so we can
        # listen to it and decide if garbage-in or garbage-config.
        if os.getenv("FLEET_VOICE_DEBUG_DUMP", "0") == "1":
            import time, wave, pathlib
            dump_dir = pathlib.Path(os.getenv("FLEET_VOICE_DEBUG_DUMP_DIR",
                "Z:/_planning/respeaker-xvf3800-alfred/debug_pcm"))
            dump_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time() * 1000)
            wav_path = dump_dir / f"utt_{ts}.wav"
            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(pcm)
            rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
            peak = float(np.max(np.abs(audio)))
            dur_s = len(audio) / SAMPLE_RATE
            logger.warning(
                "fleet_voice STT debug: dumped %s bytes=%d dur=%.2fs rms=%.4f peak=%.4f sr=%d",
                wav_path.name, len(pcm), dur_s, rms, peak, SAMPLE_RATE,
            )

        def _do_transcribe():
            model = _get_local_model()
            # Pre-gate: skip Whisper entirely if peak audio is below noise
            # floor; nothing useful to transcribe and avoids hallucinations.
            peak_amp = float(np.max(np.abs(audio))) if audio.size else 0.0
            min_peak = float(os.getenv("FLEET_VOICE_MIN_PEAK", "0.05"))
            if peak_amp < min_peak:
                logger.info(
                    "fleet_voice STT: skip silent buffer peak=%.4f < %.4f",
                    peak_amp, min_peak,
                )
                return ""
            segments, _info = model.transcribe(
                audio,
                language=WHISPER_LANGUAGE or None,
                beam_size=1,
                # Peak gate above already culls silence; trust Whisper defaults
                # otherwise. Aggressive log_prob/vad_filter/no_speech tweaks
                # rejected real speech (peak=1.0 buffers returning empty).
                vad_filter=False,
                # condition_on_previous_text causes context-bleed across drains.
                condition_on_previous_text=False,
            )
            return "".join(seg.text for seg in segments).strip()

        text = await loop.run_in_executor(None, _do_transcribe)
        # WARNING level so it shows in default uvicorn log streams.
        logger.warning("fleet_voice STT transcript (local): %r", text)
        return text

    # backend == "openai" (or anything else); fall through to cloud path
    if not OPENAI_API_KEY:
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
    logger.info("fleet_voice STT transcript (openai): %r", text)
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
