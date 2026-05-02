# Fleet voice gateway sidecar package
#
# Sibling of `fleet_gateway`. Exposes a WebSocket endpoint at
# `/v1/fleet/voice` that accepts authed connections from voice-puck
# devices (XIAO ESP32-S3 + ReSpeaker XVF3800) and the Alfred PWA.
#
# Frame protocol:
#   - inbound binary  : opaque Opus blobs, accounted by size + counter
#                       (decoded + transcribed by SAL-4003 stt.py)
#   - inbound text    : JSON control messages, validated, ack-echoed
#   - outbound binary : audio frames pushed by adapters
#                       (SAL-4005 tts.py emits 16 kHz mono 20 ms / 32 kbps
#                       Opus packets for the assistant reply; music path
#                       lands later via SAL-4014 / SAL-4015)
#   - outbound text   : JSON control messages on same schema, plus the
#                       SAL-4005 audio_start / audio_end envelopes that
#                       bracket each binary audio stream
#
# Auth: bearer token on the WS handshake, identical to
# `fleet_gateway.server`. Pre-pairing this is a static
# `FLEET_VOICE_KEY` env var; SAL-4017 swaps it for per-device JWT.
#
# Per-device session state lives in-memory and is exposed (admin-auth)
# at `GET /v1/fleet/voice/sessions` for ops.
from .server import app  # noqa: F401
