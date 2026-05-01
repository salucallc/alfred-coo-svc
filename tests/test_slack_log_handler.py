"""Tests for the Slack log forwarder.

Hits no network: ``httpx.Client``/``AsyncClient`` are monkeypatched. The
event-loop-aware emit path is exercised by calling ``emit`` from a normal
synchronous test (sync fallback) and the dedupe path is exercised by
asserting the underlying capture is called once for two identical WARNINGs
inside the window.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

import httpx
import pytest

from alfred_coo import slack_log_handler as slh
from alfred_coo.log import setup_logging
from alfred_coo.slack_log_handler import (
    DEFAULT_DEDUPE_WINDOW_SECONDS,
    SlackLogHandler,
    build_handler_from_env,
)


# ---------------------------------------------------------------------------
# Test infrastructure: capture httpx.Client.post() calls without network IO.
# ---------------------------------------------------------------------------


class _CapturedPost:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        # Slack happy-path body; SlackLogHandler doesn't actually inspect it.
        return httpx.Response(200, json={"ok": True, "ts": "1.2"})


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> _CapturedPost:
    """Patch httpx.Client so emit() runs through our sync fallback."""
    cap = _CapturedPost()

    class _FakeSyncClient:
        def __init__(self, *a: Any, **kw: Any) -> None: ...
        def __enter__(self) -> "_FakeSyncClient":
            return self
        def __exit__(self, *a: Any) -> None: ...
        def post(self, url: str, **kwargs: Any) -> httpx.Response:
            return cap(url, **kwargs)

    monkeypatch.setattr(slh.httpx, "Client", _FakeSyncClient)
    return cap


def _make_record(
    name: str = "alfred_coo.dispatch",
    level: int = logging.WARNING,
    msg: str = "silent_with_tools detected",
    exc_info: Any = None,
) -> logging.LogRecord:
    rec = logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=42,
        msg=msg,
        args=(),
        exc_info=exc_info,
    )
    return rec


# ---------------------------------------------------------------------------
# Format
# ---------------------------------------------------------------------------


def test_warning_format_has_prefix_logger_and_body(capture: _CapturedPost) -> None:
    h = SlackLogHandler(bot_token="xoxb-fake", channel_id="C0ASAKFTR1C")
    h.emit(_make_record(level=logging.WARNING, msg="gateway 500 retry"))
    assert len(capture.calls) == 1
    body = capture.calls[0]["json"]
    assert body["channel"] == "C0ASAKFTR1C"
    text = body["text"]
    # warning emoji prefix
    assert text.startswith("⚠️")
    assert "WARNING" in text
    assert "alfred_coo.dispatch" in text
    assert "gateway 500 retry" in text
    # auth header is the bot token
    assert capture.calls[0]["headers"]["Authorization"] == "Bearer xoxb-fake"


def test_error_format_uses_error_emoji(capture: _CapturedPost) -> None:
    h = SlackLogHandler(bot_token="xoxb-fake", channel_id="C0ASAKFTR1C")
    h.emit(_make_record(level=logging.ERROR, msg="MAX_TOOL_ITERATIONS hit"))
    assert len(capture.calls) == 1
    text = capture.calls[0]["json"]["text"]
    assert text.startswith("\U0001f6a8")  # 🚨
    assert "ERROR" in text
    assert "MAX_TOOL_ITERATIONS hit" in text


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------


def test_dedupe_suppresses_identical_warning_within_window(
    capture: _CapturedPost,
) -> None:
    h = SlackLogHandler(bot_token="xoxb-fake", channel_id="C0ASAKFTR1C")
    msg = "gateway 500 retry"
    h.emit(_make_record(level=logging.WARNING, msg=msg))
    h.emit(_make_record(level=logging.WARNING, msg=msg))
    # Second call must be suppressed by the 30s window.
    assert len(capture.calls) == 1
    # Sanity: window is the documented value.
    assert h.dedupe_window_seconds == DEFAULT_DEDUPE_WINDOW_SECONDS


def test_dedupe_does_not_suppress_errors(capture: _CapturedPost) -> None:
    h = SlackLogHandler(bot_token="xoxb-fake", channel_id="C0ASAKFTR1C")
    msg = "kaboom"
    h.emit(_make_record(level=logging.ERROR, msg=msg))
    h.emit(_make_record(level=logging.ERROR, msg=msg))
    # Both errors must go through — they're rare and important.
    assert len(capture.calls) == 2


def test_dedupe_releases_after_window(capture: _CapturedPost) -> None:
    # Tiny window so the test is fast.
    h = SlackLogHandler(
        bot_token="xoxb-fake",
        channel_id="C0ASAKFTR1C",
        dedupe_window_seconds=0.05,
    )
    msg = "throttled retry"
    h.emit(_make_record(level=logging.WARNING, msg=msg))
    time.sleep(0.07)
    h.emit(_make_record(level=logging.WARNING, msg=msg))
    assert len(capture.calls) == 2


# ---------------------------------------------------------------------------
# Exception info
# ---------------------------------------------------------------------------


def test_exc_info_appends_fenced_traceback(
    capture: _CapturedPost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exception info should produce a fenced code block with traceback text.

    We stub ``traceback.format_exception`` so the test doesn't depend on
    Python's real traceback formatting (which trio monkey-patches in this
    test environment, breaking the ``compact=True`` codepath).
    """
    fake_tb = (
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1, in <module>\n'
        "    raise RuntimeError('synthetic boom')\n"
        "RuntimeError: synthetic boom\n"
    )
    monkeypatch.setattr(slh.traceback, "format_exception", lambda *a, **k: [fake_tb])

    h = SlackLogHandler(bot_token="xoxb-fake", channel_id="C0ASAKFTR1C")
    # exc_info is a 3-tuple shape; the values aren't actually used because
    # format_exception is stubbed.
    rec = _make_record(
        level=logging.ERROR,
        msg="dispatch failed",
        exc_info=(RuntimeError, RuntimeError("synthetic boom"), None),
    )
    h.emit(rec)
    text = capture.calls[0]["json"]["text"]
    assert "```" in text
    assert "RuntimeError" in text
    assert "synthetic boom" in text


def test_traceback_is_truncated_to_500_chars(
    capture: _CapturedPost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even very long tracebacks must stay under Slack-friendly bounds."""
    monkeypatch.setattr(
        slh.traceback,
        "format_exception",
        lambda *a, **k: ["x" * 4000],
    )
    h = SlackLogHandler(bot_token="xoxb-fake", channel_id="C0ASAKFTR1C")
    rec = _make_record(
        level=logging.ERROR,
        msg="boom",
        exc_info=(RuntimeError, RuntimeError("oops"), None),
    )
    h.emit(rec)
    text = capture.calls[0]["json"]["text"]
    fenced_start = text.find("```")
    fenced_end = text.rfind("```")
    assert fenced_end > fenced_start
    # Truncation budget + fence framing + ellipsis fudge.
    assert fenced_end - fenced_start <= 500 + 50


# ---------------------------------------------------------------------------
# Failure tolerance
# ---------------------------------------------------------------------------


def test_slack_post_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    class _ExplodingClient:
        def __init__(self, *a: Any, **kw: Any) -> None: ...
        def __enter__(self) -> "_ExplodingClient":
            return self
        def __exit__(self, *a: Any) -> None: ...
        def post(self, *a: Any, **kw: Any) -> httpx.Response:
            # Mimic a Slack 500 by raising, which is harsher than a 500 body.
            raise httpx.HTTPError("upstream 500")

    monkeypatch.setattr(slh.httpx, "Client", _ExplodingClient)
    h = SlackLogHandler(bot_token="xoxb-fake", channel_id="C0ASAKFTR1C")
    # Must not raise.
    h.emit(_make_record(level=logging.WARNING, msg="will fail to post"))


def test_slack_500_response_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a 500 response body returned cleanly should not propagate."""

    class _Fake500Client:
        def __init__(self, *a: Any, **kw: Any) -> None: ...
        def __enter__(self) -> "_Fake500Client":
            return self
        def __exit__(self, *a: Any) -> None: ...
        def post(self, *a: Any, **kw: Any) -> httpx.Response:
            return httpx.Response(500, text="upstream boom")

    monkeypatch.setattr(slh.httpx, "Client", _Fake500Client)
    h = SlackLogHandler(bot_token="xoxb-fake", channel_id="C0ASAKFTR1C")
    h.emit(_make_record(level=logging.WARNING, msg="server error path"))


# ---------------------------------------------------------------------------
# Opt-in wire-in
# ---------------------------------------------------------------------------


def test_build_handler_returns_none_without_token() -> None:
    assert build_handler_from_env(bot_token=None, channel_id="C0ASAKFTR1C") is None
    assert build_handler_from_env(bot_token="", channel_id="C0ASAKFTR1C") is None


def test_build_handler_returns_none_without_channel() -> None:
    assert build_handler_from_env(bot_token="xoxb-fake", channel_id=None) is None
    assert build_handler_from_env(bot_token="xoxb-fake", channel_id="") is None


def test_build_handler_returns_handler_when_both_present() -> None:
    h = build_handler_from_env(bot_token="xoxb-fake", channel_id="C0ASAKFTR1C")
    assert isinstance(h, SlackLogHandler)
    assert h.bot_token == "xoxb-fake"
    assert h.channel_id == "C0ASAKFTR1C"
    assert h.level == logging.WARNING


def test_setup_logging_without_token_does_not_attach_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN_ALFRED", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_LOG_CHANNEL_ID", raising=False)

    setup_logging("INFO", "json")
    root = logging.getLogger()
    assert not any(isinstance(h, SlackLogHandler) for h in root.handlers)


def test_setup_logging_attaches_handler_with_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN_ALFRED", "xoxb-fake-from-test")
    monkeypatch.setenv("SLACK_LOG_CHANNEL_ID", "C0ASAKFTR1C")

    setup_logging("INFO", "json")
    root = logging.getLogger()
    slack_handlers = [h for h in root.handlers if isinstance(h, SlackLogHandler)]
    assert len(slack_handlers) == 1
    assert slack_handlers[0].bot_token == "xoxb-fake-from-test"
    assert slack_handlers[0].channel_id == "C0ASAKFTR1C"

    # Cleanup so subsequent tests aren't polluted by a real-looking handler.
    for h in slack_handlers:
        root.removeHandler(h)
