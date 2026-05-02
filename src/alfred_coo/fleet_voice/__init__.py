# Fleet voice gateway sidecar package
#
# Sibling of `fleet_gateway`. Exposes a WebSocket endpoint at
# `/v1/fleet/voice` that accepts authed connections from voice-puck
# devices (XIAO ESP32-S3 + ReSpeaker XVF3800) and the Alfred PWA.
#
# Frame protocol (skeleton — STT plugs in via SAL-4003, TTS via SAL-4005):
#   - inbound binary  : opaque Opus blobs, accounted by size + counter
#   - inbound text    : JSON control messages, validated, ack-echoed
#   - outbound binary : audio frames pushed by adapters (TTS / music)
#   - outbound text   : JSON control messages on same schema
#
# Auth: bearer token on the WS handshake, identical to
# `fleet_gateway.server`. Pre-pairing this is a static
# `FLEET_VOICE_KEY` env var; SAL-4017 swaps it for per-device JWT.
#
# Per-device session state lives in-memory and is exposed (admin-auth)
# at `GET /v1/fleet/voice/sessions` for ops.
from .server import app  # noqa: F401
