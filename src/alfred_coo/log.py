"""
Structured logging setup for alfred_coo application.
"""

import logging
import os
from typing import Optional

try:
    from pythonjsonlogger import jsonlogger
    JSONLOGGER_AVAILABLE = True
except ImportError:
    JSONLOGGER_AVAILABLE = False

from alfred_coo.slack_log_handler import build_handler_from_env


def setup_logging(level: str = "INFO", fmt: Optional[str] = None) -> logging.Logger:
    """Configure root logger and return alfred_coo child logger."""
    if fmt is None:
        fmt = os.environ.get("LOG_FORMAT", "json")

    # Create formatter
    if fmt.lower() == "json" and JSONLOGGER_AVAILABLE:
        formatter = jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
            timestamp=True
        )
    else:
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z"
        )

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Clear existing handlers
    root_logger.handlers.clear()

    # Add handler with our formatter
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)

    # Optional Slack forwarder for WARNING+ events. Opt-in via
    # SLACK_BOT_TOKEN_ALFRED + SLACK_LOG_CHANNEL_ID (defaults to #batcave).
    # Failure to attach is non-fatal — daemon must still boot if Slack is
    # unconfigured or the handler module breaks.
    slack_token = (
        os.environ.get("SLACK_BOT_TOKEN_ALFRED")
        or os.environ.get("SLACK_BOT_TOKEN")
    )
    slack_channel = os.environ.get("SLACK_LOG_CHANNEL_ID", "C0ASAKFTR1C")
    try:
        slack_handler = build_handler_from_env(
            bot_token=slack_token,
            channel_id=slack_channel,
            level=logging.WARNING,
        )
        if slack_handler is not None:
            root_logger.addHandler(slack_handler)
    except Exception:  # noqa: BLE001 — never break logging setup
        pass

    # Return child logger
    return logging.getLogger("alfred_coo")
