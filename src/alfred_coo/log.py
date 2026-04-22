"""
Structured logging setup for alfred_coo application.
"""

import json
import logging
import os
from typing import Optional

try:
    from pythonjsonlogger import jsonlogger
    JSONLOGGER_AVAILABLE = True
except ImportError:
    JSONLOGGER_AVAILABLE = False


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
    
    # Return child logger
    return logging.getLogger("alfred_coo")
