"""fleet_voice WebSocket gateway (SAL-4002, P1.4).

Accepts authed WebSocket connections at `/v1/fleet/voice` from voice-puck
devices and the Alfred PWA. Plumbs audio (binary Opus blobs) and a JSON
control plane on the same socket. STT, TTS, and music orchestration plug
in later (SAL-4003 / SAL-4005 / SAL-4014).

Design choices (mirrors `alfred_coo.fleet_gateway.server`):
  * Standalone FastAPI app exposed as `app`, deployable as a sidecar with
    `uvicorn alfred_coo.fleet_voice.server:app`. soul-svc stays HTTP-only.
  * Bearer-token auth on the WS handshake, identical pattern to
    `fleet_gateway.server`. The token is read from header
    `Authorization: Bearer <token>` and compared to `FLEET_VOICE_KEY`
    (env var, default `valid-key` for tests). Per-device JWT lands in
    SAL-4017 once the portal pairing flow ships; until then the bearer
    token IS the device identity and the device_id is derived from a
    `?device_id=` query param so the session table can disambiguate
    multi-puck households.
  * Admin endpoint `GET /v1/fleet/voice/sessions` requires
    `Authorization: Bearer <FLEET_VOICE_ADMIN_KEY>` (separate key so the
    device fleet never sees admin scope). Returns a snapshot of in-memory
    session state.

Frame protocol — skeleton:

  Inbound binary:
    Treated as opaque Opus blobs. We log size + bump frames_in /
    bytes_in counters. STT adapter (SAL-4003) will subscribe to these.

  Inbound text:
    Parsed as JSON. Required keys: `type` (str), `seq` (int). Optional:
    `payload` (any). Malformed (not JSON, missing keys, wrong types) →
    we send `{"type":"error","reason":"...","seq":<best-effort>}` and
    keep the socket open so the client can recover. Hello/ping are
    ack'd; everything else is echo-ack'd until SAL-4003 lands real
    handlers.

  Outbound:
    `send_binary(...)` for audio frames; `send_text(...)` for JSON
    control. Both routed through `VoiceSession.send_*` so accounting
    stays correct.

  Lifecycle:
    1. Handshake: ws.accept() then auth check; reject with 1008 on bad
       token. Send `{"type":"hello", ...}` once auth passes.
    2. Steady state: server pings every 15s; client should pong (text
       JSON `{"type":"pong","seq":...}`) within `PING_TIMEOUT_S`.
    3. Close: graceful 1000 on either side; we update `disconnect_at`.

Tests in `tests/test_fleet_voice_ws.py` cover handshake auth,
ping/pong, opus round-trip, control echo, malformed JSON, and the
session snapshot endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

VALID_KEY = os.getenv("FLEET_VOICE_KEY", "valid-key")
"""Device-side bearer token (placeholder until SAL-4017 device JWT lands)."""

ADMIN_KEY = os.getenv("FLEET_VOICE_ADMIN_KEY", "admin-key")
"""Admin bearer token for the sessions snapshot endpoint."""

PING_INTERVAL_S = float(os.getenv("FLEET_VOICE_PING_INTERVAL_S", "15"))
"""Server-driven ping cadence. 15s matches the fleet_gateway convention."""

PING_TIMEOUT_S = float(os.getenv("FLEET_VOICE_PING_TIMEOUT_S", "45"))
"""If `last_seen` ages past this without a client message, we close 1011."""

PROTOCOL_VERSION = "1"
"""Bumped when the inbound JSON schema changes incompatibly."""


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


@dataclass
class VoiceSession:
    """Per-connection state for one voice-puck (or PWA) WebSocket.

    Held in `SESSIONS` keyed by `session_id`. Counters update on every
    frame. `last_seen` advances on any inbound message (binary or text),
    so the keepalive checker can decide whether to fire a ping or kill
    the socket.

    Once SAL-4017 lands, `device_id` becomes the JWT subject claim. For
    now it falls back to the `?device_id=` query param, or "anon-<uuid>"
    if the client omits one.

    SAL-4003 (STT) extends this with the per-session Opus accumulator:
      * `opus_buffer` — list of binary Opus frames received since the
        last drain. Drained by `start_utterance` (caller-driven mode)
        or by the `_maybe_autodrain` heuristic on natural silence.
      * `transcript_seq` — outbound counter for `transcript` /
        `assistant_text` frames so the device can detect drops.
    """

    session_id: str
    device_id: str
    ws: WebSocket
    connect_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    disconnect_at: float | None = None
    frames_in: int = 0
    frames_out: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    last_seq_in: int | None = None
    opus_buffer: list[bytes] = field(default_factory=list)
    transcript_seq: int = 0

    async def send_text(self, payload: dict[str, Any]) -> None:
        """Send a JSON control message; updates outbound accounting."""
        body = json.dumps(payload, separators=(",", ":"))
        await self.ws.send_text(body)
        self.frames_out += 1
        self.bytes_out += len(body.encode("utf-8"))

    async def send_binary(self, payload: bytes) -> None:
        """Send a binary frame (e.g. TTS Opus output) and account for it."""
        await self.ws.send_bytes(payload)
        self.frames_out += 1
        self.bytes_out += len(payload)

    def snapshot(self) -> dict[str, Any]:
        """Pure dict view used by the admin sessions endpoint."""
        return {
            "session_id": self.session_id,
            "device_id": self.device_id,
            "connect_at": self.connect_at,
            "last_seen": self.last_seen,
            "disconnect_at": self.disconnect_at,
            "frames_in": self.frames_in,
            "frames_out": self.frames_out,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "last_seq_in": self.last_seq_in,
        }


SESSIONS: dict[str, VoiceSession] = {}
"""In-memory session registry. Keyed by random per-connect session_id."""


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _extract_bearer(authz: str | None) -> str | None:
    """Pull the token out of an `Authorization: Bearer <token>` header.

    Returns None if the header is missing or malformed. Case-sensitive
    on the `Bearer ` prefix to match fleet_gateway behaviour.
    """
    if not authz:
        return None
    if not authz.startswith("Bearer "):
        return None
    return authz[len("Bearer "):].strip() or None


def _check_device_auth(ws: WebSocket) -> bool:
    """Validate the device-side bearer token on a fresh WS handshake."""
    token = _extract_bearer(ws.headers.get("authorization"))
    return token is not None and token == VALID_KEY


def _check_admin_auth(request: Request) -> bool:
    """Validate the admin bearer token for HTTP-side endpoints."""
    token = _extract_bearer(request.headers.get("authorization"))
    return token is not None and token == ADMIN_KEY


# ---------------------------------------------------------------------------
# Inbound message handling
# ---------------------------------------------------------------------------


def _parse_control(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse an inbound text frame as a control message.

    Returns `(message, None)` on success, or `(None, reason)` on
    failure. We accept any JSON object that has `type` (str) and `seq`
    (int >= 0); the rest is opaque payload that downstream adapters
    interpret. This keeps the gateway codec-agnostic.
    """
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return None, "not_json"
    if not isinstance(msg, dict):
        return None, "not_object"
    typ = msg.get("type")
    seq = msg.get("seq")
    if not isinstance(typ, str) or not typ:
        return None, "missing_type"
    if not isinstance(seq, int) or seq < 0:
        return None, "missing_or_invalid_seq"
    return msg, None


async def _drain_and_transcribe(session: VoiceSession, *, source: str) -> None:
    """Pop the session Opus buffer, run STT, emit transcript + soul reply.

    `source` is the trigger that caused the drain ("start_utterance",
    "end_utterance", "auto_vad") and is logged + included in the
    outbound transcript frame so the device knows whether the server
    cut the segment itself or honored the device's hint.

    Lazy-imports `stt` so the module can load on bare boxes without
    libopus / webrtcvad. Errors are caught and surface as a single
    `error` frame; the WS stays open.
    """
    if not session.opus_buffer:
        return
    frames = session.opus_buffer
    session.opus_buffer = []
    try:
        from . import stt  # lazy: keeps module import-clean on dev boxes
        pcm = stt.decode_opus_buffer(frames)
        if not pcm:
            return
        segments = stt.segment_on_vad(pcm, stt.SAMPLE_RATE)
        if not segments:
            return
        merged = b"".join(segments)
        text = await stt.transcribe(merged)
        if not text:
            return
        session.transcript_seq += 1
        await session.send_text({
            "type": "transcript",
            "seq": session.transcript_seq,
            "text": text,
            "source": source,
            "segment_count": len(segments),
        })
        reply = await stt.post_to_soul(text, session_id=session.device_id)
        if reply:
            session.transcript_seq += 1
            await session.send_text({
                "type": "assistant_text",
                "seq": session.transcript_seq,
                "text": reply,
            })
    except Exception as exc:
        logger.exception(
            "fleet_voice STT drain failed session=%s source=%s frames=%d",
            session.session_id, source, len(frames),
        )
        try:
            await session.send_text({
                "type": "error",
                "reason": "stt_drain_failed",
                "detail": str(exc)[:200],
                "seq": session.transcript_seq,
            })
        except Exception:
            pass


async def _handle_control(session: VoiceSession, msg: dict[str, Any]) -> None:
    """Dispatch a parsed control message.

    Handlers:
      * `hello` → reply with `welcome` carrying server protocol version
      * `pong` → no-op (last_seen already bumped by the receive loop)
      * `start_utterance` (SAL-4003) → drain pending Opus buffer right
        now; device uses this when its on-board VAD fires the begin-
        of-utterance edge. We accept that any audio still in flight
        before this control arrives gets transcribed as "previous
        utterance"; on re-thinking we could reset the buffer instead,
        but draining is the safer default (no audio gets dropped).
      * `end_utterance` (SAL-4003) → same drain, different label so we
        know in logs whether the device thinks the user just stopped
        talking versus just started.
      * everything else → ack-echo so the client can verify round-trip

    SAL-4005 (TTS) and SAL-4014 (music orchestrator) will register
    additional handlers here.
    """
    typ = msg["type"]
    seq = msg["seq"]
    if typ == "hello":
        await session.send_text({
            "type": "welcome",
            "seq": seq,
            "session_id": session.session_id,
            "device_id": session.device_id,
            "protocol_version": PROTOCOL_VERSION,
        })
        return
    if typ == "pong":
        return
    if typ in ("start_utterance", "end_utterance"):
        # Ack first so the device knows we received the edge marker even
        # if the drain itself fails or returns empty.
        await session.send_text({"type": "ack", "seq": seq, "echo_type": typ})
        await _drain_and_transcribe(session, source=typ)
        return
    # Default: echo-ack every other type until real handlers land.
    await session.send_text({"type": "ack", "seq": seq, "echo_type": typ})


# ---------------------------------------------------------------------------
# Keepalive
# ---------------------------------------------------------------------------


async def _keepalive_loop(session: VoiceSession) -> None:
    """Server-driven ping loop. Closes the socket on inactivity timeout.

    Runs as a sibling task to the inbound receive loop. Cancelled when
    the receive loop returns (graceful disconnect or auth failure).
    """
    seq = 0
    try:
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            now = time.time()
            if now - session.last_seen > PING_TIMEOUT_S:
                logger.info(
                    "fleet_voice keepalive: closing stale session %s "
                    "(last_seen %.1fs ago)",
                    session.session_id,
                    now - session.last_seen,
                )
                try:
                    await session.ws.close(code=1011, reason="ping_timeout")
                except Exception:
                    pass
                return
            try:
                await session.send_text({"type": "ping", "seq": seq})
                seq += 1
            except Exception:
                # Socket already gone; receive loop will tear down.
                return
    except asyncio.CancelledError:
        return


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="alfred-coo fleet_voice", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Liveness probe + a cheap session count for at-a-glance ops."""
    return {"ok": True, "active_sessions": sum(
        1 for s in SESSIONS.values() if s.disconnect_at is None
    )}


@app.get("/v1/fleet/voice/sessions")
async def list_sessions(request: Request) -> dict[str, Any]:
    """Admin snapshot of the in-memory session table.

    Bearer-auth via `FLEET_VOICE_ADMIN_KEY`. Returns *all* sessions
    (including recently-disconnected ones) so ops can see counters
    from a session that died seconds ago. SAL-4030 will move this to
    a metrics scrape endpoint; until then this is the only window in.
    """
    if not _check_admin_auth(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    return {
        "sessions": [s.snapshot() for s in SESSIONS.values()],
        "count": len(SESSIONS),
    }


@app.websocket("/v1/fleet/voice")
async def fleet_voice(ws: WebSocket) -> None:
    """Voice gateway WebSocket entry point.

    Lifecycle:
      1. Accept the upgrade so we can speak the WS protocol; auth check
         happens AFTER accept because we need to use ws.close() to send
         a clean 1008. (Same pattern as fleet_gateway.server.)
      2. Validate bearer token against `FLEET_VOICE_KEY`. Reject with
         policy-violation 1008 on mismatch.
      3. Allocate a `VoiceSession`, push hello, spawn keepalive task,
         enter the receive loop.
      4. Receive loop dispatches binary vs text, updates accounting,
         hands control messages off to `_handle_control`.
      5. On disconnect (graceful or otherwise), cancel keepalive,
         stamp disconnect_at, leave the session in SESSIONS so the
         admin endpoint can report final counters until the next
         restart. SAL-4030 will add an LRU eviction policy.
    """
    await ws.accept()
    if not _check_device_auth(ws):
        await ws.close(code=1008, reason="unauthorized")
        return

    device_id = ws.query_params.get("device_id") or f"anon-{uuid.uuid4().hex[:8]}"
    session_id = uuid.uuid4().hex
    session = VoiceSession(session_id=session_id, device_id=device_id, ws=ws)
    SESSIONS[session_id] = session

    # Initial hello so the client knows we're alive and learns its
    # session_id. Counts as session.frames_out=1.
    try:
        await session.send_text({
            "type": "hello",
            "seq": 0,
            "session_id": session_id,
            "device_id": device_id,
            "protocol_version": PROTOCOL_VERSION,
            "ping_interval_s": PING_INTERVAL_S,
        })
    except Exception:
        SESSIONS.pop(session_id, None)
        return

    keepalive = asyncio.create_task(
        _keepalive_loop(session), name=f"fleet-voice-keepalive-{session_id}"
    )

    try:
        while True:
            event = await ws.receive()
            session.last_seen = time.time()
            etype = event.get("type")
            if etype == "websocket.disconnect":
                break
            # Binary frame → opaque Opus blob.
            # SAL-4003 (STT): accumulate into per-session opus_buffer for
            # drain on `start_utterance` / `end_utterance` control. The
            # blob is appended verbatim; we trust the device to send one
            # Opus packet per binary frame (matches firmware ticket
            # SAL-4001's encoder loop).
            if "bytes" in event and event["bytes"] is not None:
                blob: bytes = event["bytes"]
                session.frames_in += 1
                session.bytes_in += len(blob)
                session.opus_buffer.append(blob)
                logger.debug(
                    "fleet_voice rx binary session=%s frame=%d bytes=%d buffered=%d",
                    session_id, session.frames_in, len(blob),
                    len(session.opus_buffer),
                )
                continue
            # Text frame → JSON control plane.
            if "text" in event and event["text"] is not None:
                raw: str = event["text"]
                session.frames_in += 1
                session.bytes_in += len(raw.encode("utf-8"))
                msg, err = _parse_control(raw)
                if err is not None:
                    # Best-effort echo of seq if it looked numeric.
                    bad_seq = -1
                    try:
                        maybe = json.loads(raw)
                        if isinstance(maybe, dict) and isinstance(maybe.get("seq"), int):
                            bad_seq = maybe["seq"]
                    except Exception:
                        pass
                    await session.send_text({
                        "type": "error",
                        "reason": err,
                        "seq": bad_seq,
                    })
                    continue
                assert msg is not None  # for type narrowing
                session.last_seq_in = msg["seq"]
                await _handle_control(session, msg)
                continue
            # Anything else (e.g. websocket.connect re-fire) is ignored.
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception(
            "fleet_voice session %s receive loop crashed", session_id
        )
    finally:
        keepalive.cancel()
        try:
            await keepalive
        except (asyncio.CancelledError, Exception):
            pass
        session.disconnect_at = time.time()
        # NOTE: we keep the entry in SESSIONS so the admin endpoint can
        # serve final counters. Eviction policy lands in SAL-4030.
