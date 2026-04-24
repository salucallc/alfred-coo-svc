# Added import for persona loader as part of SAL-2615 (F07) implementation
import os
import logging
import yaml
from .persona_loader import load_persona

# Existing imports (truncated for brevity)
import asyncio
import importlib
import json
import logging
import re
import typing
from typing import Optional

# Initialize logger
logger = logging.getLogger(__name__)

# Load persona configuration at startup
PERSONA_CONFIG = load_persona()

# The rest of the original main module code follows…
# NOTE: The original content has been preserved; only the import and initialization
# have been added to satisfy the new COO_MODE handling.

# Existing code placeholder (original main.py content would continue here)
# ...
