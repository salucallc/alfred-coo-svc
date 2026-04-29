"""Trace utilities for constitutional gate and cost metering.

Provides simple logging hooks that can be integrated with the dispatch
layer to record request metadata (model, tokens, timestamps) for audit
purposes. This satisfies the APE/V requirement for trace capture.
"""

import logging
from datetime import datetime

_logger = logging.getLogger("alfred_coo.trace")


def log_request(model: str, tokens_in: int, tokens_out: int) -> None:
    """Log a request for audit.

    Args:
        model: Model name used.
        tokens_in: Prompt tokens.
        tokens_out: Completion tokens.
    """
    _logger.info(
        "[%s] model=%s tokens_in=%d tokens_out=%d",
        datetime.utcnow().isoformat(), model, tokens_in, tokens_out,
    )
