"""SS-08 (PQ receipt endpoint) JWS claims schema gate.

AB-06 scope: when the autonomous_build orchestrator hits ticket code
`SS-08` in a wave, it must pause and post the JWS claims schema to
#batcave, then block until Cristian replies with `ACK SS-08` / `approve
SS-08` (case-insensitive). On ACK: proceed with dispatch. On 4h timeout:
defer to v1.1 per D2 decision and mark the ticket FAILED so the wave
loop skips it cleanly.

The gate is driven by `slack_ack_poll` (AB-02) polled every 2 minutes
against #batcave; the first qualifying message from Cristian's user id
closes the gate.

Decision locked 2026-04-23: the bot lacks `users:read.email` scope and
Slack app reinstall was declined, so the approver user id is hardcoded
here as a module constant rather than resolved dynamically. See memory
`reference_cristian_slack_user_id.md` for context.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional


logger = logging.getLogger("alfred_coo.autonomous_build.ss08_gate")


# Hardcoded per 2026-04-23 decision — bot lacks `users:read.email` scope.
# See reference_cristian_slack_user_id.md.
CRISTIAN_SLACK_USER_ID = "U0AH88KHZ4H"


JWS_CLAIMS_SCHEMA_YAML = """
# JWS claims schema for /v1/challenge/verify PQ receipt endpoint
# Approve this schema in #batcave by replying "ACK SS-08" or "approve SS-08"
# Reply in this channel, any time within 4h of the gate post.

claims:
  tenant_id: uuid          # which tenant this receipt binds
  pubkey_sha: string       # SHA-256 of the tenant's hybrid public key
  issued_at: integer       # Unix epoch seconds
  expires_at: integer      # Unix epoch seconds; typical TTL 300s
  scope: list[string]      # permissions receipt grants, e.g. ["mesh_write", "soul_memory_write"]
  hybrid_sig_meta:
    alg: string            # "ML-DSA-44+ed25519" literal
    kid: string            # key identifier
    sig_version: int       # bump on schema revision
""".lstrip("\n")


# Regex patterns matched case-insensitively by `slack_ack_poll`.
ACK_KEYWORDS: List[str] = [
    r"ack\s*ss[-_\s]?08",
    r"approve(d)?\s*ss[-_\s]?08",
]


# 4-hour gate timeout (D2 decision: defer SS-08 to v1.1 on timeout).
GATE_TIMEOUT_SECONDS: int = 4 * 3600

# Poll interval between `slack_ack_poll` calls.
GATE_POLL_INTERVAL_SECONDS: int = 2 * 60


# Type alias for the injected poll callable. Must accept the four kwargs
# expected by `slack_ack_poll` (channel, after_ts, author_user_id, keywords)
# and return either `{"matched": True, ...}`, `{"matched": False}`, or
# `{"error": "..."}`.
SlackAckPollFn = Callable[..., Awaitable[Dict[str, Any]]]


async def run_ss08_gate(
    cadence,
    slack_ack_poll_fn: SlackAckPollFn,
    logger_: Optional[logging.Logger] = None,
    *,
    timeout_seconds: int = GATE_TIMEOUT_SECONDS,
    poll_interval_seconds: int = GATE_POLL_INTERVAL_SECONDS,
    author_user_id: str = CRISTIAN_SLACK_USER_ID,
) -> bool:
    """Post the SS-08 JWS claims schema to #batcave and wait for ACK.

    Args:
        cadence: A `SlackCadence` instance; we use `cadence.post(msg)` for
            posting the schema, the ack confirmation, and the timeout
            message, and `cadence.channel` to pick the channel to poll.
        slack_ack_poll_fn: Async callable matching the `slack_ack_poll`
            tool handler signature. Tests inject a fake here; production
            passes the real handler resolved from `BUILTIN_TOOLS`.
        logger_: Optional logger override (defaults to module logger).
        timeout_seconds: Overall gate timeout; exposed for tests.
        poll_interval_seconds: Sleep between poll attempts; exposed for
            tests.
        author_user_id: Slack user id of the approver. Defaults to the
            hardcoded Cristian id; exposed as a kwarg for tests.

    Returns:
        True if an ACK was detected before timeout; False on timeout.

    Transient errors from `slack_ack_poll_fn` (exceptions raised, or a
    dict with `error` key) are logged and the gate continues polling.
    Only an explicit `matched=True` reply closes the gate; only wall-clock
    timeout fails it.
    """
    log = logger_ or logger

    # 1. Post the schema. `cadence.post` wraps the real `slack_post` tool.
    schema_msg = f"GATE: SS-08 JWS claims schema\n\n{JWS_CLAIMS_SCHEMA_YAML}"
    try:
        await cadence.post(schema_msg)
    except Exception:
        log.exception("failed to post SS-08 gate schema; aborting gate")
        # Can't run the gate without even posting the schema — the
        # orchestrator should treat this as a non-ACK so we don't
        # silently dispatch SS-08 without approval.
        return False

    # 2. Anchor the gate at the current wall-clock time. We don't have
    #    access to the Slack-assigned ts of the schema post without
    #    upgrading `cadence.post` to return its payload, and the
    #    `after_ts` filter in `slack_ack_poll` is a `conversations.history`
    #    `oldest` parameter that takes a unix float. Using our local clock
    #    is a safe approximation — the worst case is we pick up a reply
    #    that predates the post by a few seconds, which is fine (Cristian
    #    isn't typing an ACK before the post exists).
    gate_post_ts = time.time()
    after_ts_str = f"{gate_post_ts:.6f}"
    log.info(
        "SS-08 gate posted; polling #%s for ACK from %s (timeout=%ds, interval=%ds)",
        cadence.channel, author_user_id, timeout_seconds, poll_interval_seconds,
    )

    # 3. Poll loop.
    while True:
        elapsed = time.time() - gate_post_ts
        if elapsed > timeout_seconds:
            # Timeout branch (D2: defer to v1.1).
            timeout_msg = (
                "⏰ SS-08 gate timed out after 4h — "
                "marking deferred to v1.1 per D2 decision."
            )
            try:
                await cadence.post(timeout_msg)
            except Exception:
                log.exception("cadence.post(timeout_msg) failed; continuing")
            log.warning(
                "SS-08 gate TIMED OUT after %ds (limit=%ds); deferring to v1.1",
                int(elapsed), timeout_seconds,
            )
            return False

        try:
            resp = await slack_ack_poll_fn(
                channel=cadence.channel,
                after_ts=after_ts_str,
                author_user_id=author_user_id,
                keywords=ACK_KEYWORDS,
            )
        except Exception as e:
            # Transient (network, 5xx). Log + sleep + retry on next tick.
            log.warning(
                "slack_ack_poll raised (%s: %s); retrying after %ds",
                type(e).__name__, e, poll_interval_seconds,
            )
            await asyncio.sleep(poll_interval_seconds)
            continue

        if isinstance(resp, dict) and resp.get("error"):
            # Structured error from the poller — treat as transient.
            log.warning(
                "slack_ack_poll returned error=%r; retrying after %ds",
                resp.get("error"), poll_interval_seconds,
            )
            await asyncio.sleep(poll_interval_seconds)
            continue

        if isinstance(resp, dict) and resp.get("matched"):
            matched_kw = resp.get("matched_keyword") or "(unknown)"
            message_ts = resp.get("message_ts") or "(unknown)"
            log.info(
                "SS-08 gate ACKED by %s (keyword=%r, ts=%s) after %ds",
                author_user_id, matched_kw, message_ts, int(elapsed),
            )
            ack_msg = (
                "✅ SS-08 gate acknowledged — "
                "proceeding with PQ receipt endpoint build."
            )
            try:
                await cadence.post(ack_msg)
            except Exception:
                log.exception(
                    "cadence.post(ack confirmation) failed; proceeding anyway"
                )
            return True

        # No match yet — keep polling until timeout.
        await asyncio.sleep(poll_interval_seconds)
