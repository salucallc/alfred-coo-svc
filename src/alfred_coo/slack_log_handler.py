"""Slack log forwarder for the alfred-coo daemon.

Forwards WARNING+ records from any ``alfred_coo.*`` logger to a Slack channel
(default ``#batcave``) so substrate state is visible in real time without
``ssh + journalctl + grep`` cycles.

Design constraints (see PR ``feat/slack-log-handler``):

* **Non-blocking.** ``emit`` schedules the POST on the running event loop when
  one exists, otherwise falls back to a tight-timeout sync ``httpx.Client``.
  A Slack failure is *swallowed* and surfaced via ``stderr`` only — we never
  let logging blow up the daemon.
* **Dedupe window.** Identical WARNING messages within
  ``DEFAULT_DEDUPE_WINDOW_SECONDS`` are suppressed (gateway 500 retries fire
  ~5x per dispatch and would flood ``#batcave``). ERROR records bypass dedupe
  because they're rare and important.
* **Opt-in.** ``setup_logging`` only attaches the handler when
  ``SLACK_BOT_TOKEN_ALFRED`` is set. Tests must not hit the network.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import sys
import time
import traceback
from typing import Dict, Optional

import httpx

SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
DEFAULT_DEDUPE_WINDOW_SECONDS = 30.0
SYNC_TIMEOUT_SECONDS = 3.0
TRACEBACK_TRUNCATE_CHARS = 500

_LEVEL_EMOJI: Dict[int, str] = {
    logging.WARNING: "⚠️",  # warning sign
    logging.ERROR: "\U0001f6a8",  # police car light
    logging.CRITICAL: "\U0001f6a8",
}


def _level_prefix(levelno: int) -> str:
    return _LEVEL_EMOJI.get(levelno, "ℹ️")  # info


def _format_traceback(exc_info) -> str:
    if not exc_info:
        return ""
    try:
        tb_text = "".join(traceback.format_exception(*exc_info))
    except Exception:  # pragma: no cover — defensive
        return ""
    if len(tb_text) > TRACEBACK_TRUNCATE_CHARS:
        tb_text = tb_text[: TRACEBACK_TRUNCATE_CHARS - 3] + "..."
    return f"\n```\n{tb_text}\n```"


class SlackLogHandler(logging.Handler):
    """``logging.Handler`` that forwards records to Slack ``chat.postMessage``.

    Parameters
    ----------
    bot_token:
        Slack ``xoxb-`` bot token. Required.
    channel_id:
        Channel ID (e.g. ``C0ASAKFTR1C`` for ``#batcave``). Required.
    level:
        Minimum level to forward. Defaults to ``WARNING``.
    dedupe_window_seconds:
        Identical WARNING messages within this window are suppressed. ERRORs
        are not deduped. Set to ``0`` to disable.
    """

    def __init__(
        self,
        bot_token: str,
        channel_id: str,
        level: int = logging.WARNING,
        dedupe_window_seconds: float = DEFAULT_DEDUPE_WINDOW_SECONDS,
    ) -> None:
        super().__init__(level=level)
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.dedupe_window_seconds = dedupe_window_seconds
        # message_text -> last_emit_monotonic_ts
        self._last_emit_ts: Dict[str, float] = {}

    # ---- formatting -----------------------------------------------------

    def _format_message(self, record: logging.LogRecord) -> str:
        prefix = _level_prefix(record.levelno)
        ts_iso = (
            _dt.datetime.fromtimestamp(record.created, tz=_dt.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        try:
            base_msg = record.getMessage()
        except Exception:  # pragma: no cover — defensive
            base_msg = str(record.msg)
        body = (
            f"{prefix} *{record.levelname}* `{record.name}` {ts_iso}\n{base_msg}"
        )
        body += _format_traceback(record.exc_info)
        return body

    # ---- dedupe ---------------------------------------------------------

    def _should_suppress(self, record: logging.LogRecord) -> bool:
        if self.dedupe_window_seconds <= 0:
            return False
        if record.levelno >= logging.ERROR:
            # Never drop ERROR/CRITICAL even if duplicate.
            return False
        try:
            key = record.getMessage()
        except Exception:
            key = str(record.msg)
        now = time.monotonic()
        last = self._last_emit_ts.get(key)
        if last is not None and (now - last) < self.dedupe_window_seconds:
            return True
        self._last_emit_ts[key] = now
        # Opportunistic GC so the dict doesn't grow without bound.
        if len(self._last_emit_ts) > 256:
            cutoff = now - self.dedupe_window_seconds
            self._last_emit_ts = {
                k: v for k, v in self._last_emit_ts.items() if v >= cutoff
            }
        return False

    # ---- POST -----------------------------------------------------------

    def _build_payload(self, text: str) -> Dict[str, str]:
        return {"channel": self.channel_id, "text": text}

    async def _post_async(self, text: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=SYNC_TIMEOUT_SECONDS) as client:
                await client.post(
                    SLACK_POST_MESSAGE_URL,
                    headers={
                        "Authorization": f"Bearer {self.bot_token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json=self._build_payload(text),
                )
        except Exception as exc:  # noqa: BLE001 — never raise from logging
            sys.stderr.write(
                f"[SlackLogHandler] async post failed: {type(exc).__name__}: {exc}\n"
            )

    def _post_sync(self, text: str) -> None:
        try:
            with httpx.Client(timeout=SYNC_TIMEOUT_SECONDS) as client:
                client.post(
                    SLACK_POST_MESSAGE_URL,
                    headers={
                        "Authorization": f"Bearer {self.bot_token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json=self._build_payload(text),
                )
        except Exception as exc:  # noqa: BLE001 — never raise from logging
            sys.stderr.write(
                f"[SlackLogHandler] sync post failed: {type(exc).__name__}: {exc}\n"
            )

    # ---- emit -----------------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        """Forward ``record`` to Slack. Never raises."""
        try:
            if self._should_suppress(record):
                return
            text = self._format_message(record)

            # Prefer non-blocking dispatch when an event loop is running.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is not None and loop.is_running():
                # Schedule and forget. asyncio retains the task internally
                # (via the loop) until completion; we don't need the handle.
                loop.create_task(self._post_async(text))
            else:
                self._post_sync(text)
        except Exception as exc:  # noqa: BLE001 — defense-in-depth
            sys.stderr.write(
                f"[SlackLogHandler] emit failed: {type(exc).__name__}: {exc}\n"
            )


def build_handler_from_env(
    bot_token: Optional[str],
    channel_id: Optional[str],
    level: int = logging.WARNING,
) -> Optional[SlackLogHandler]:
    """Return a ready-to-attach handler iff both token and channel are set.

    Used by :func:`alfred_coo.log.setup_logging` to keep the wire-in opt-in.
    """
    if not bot_token or not channel_id:
        return None
    return SlackLogHandler(
        bot_token=bot_token,
        channel_id=channel_id,
        level=level,
    )
