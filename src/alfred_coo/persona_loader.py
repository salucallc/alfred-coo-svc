"""Persona loader for alfred-coo-svc.

Loads the appropriate persona based on the ``COO_MODE`` environment variable.
Supported values are ``hub`` and ``endpoint``. If the variable is unset or
contains an unknown value, the loader defaults to the ``hub`` persona.

The loader is deliberately lightweight: it does not import heavy runtime
dependencies, and it raises a clear ``RuntimeError`` if the requested
persona is not defined in the registry. This matches the expectations of
ticket SAL-2615, which requires a new ``persona_loader.py`` file to be
created and used by ``main.py``.
"""

import os
from typing import Optional

from .persona import get_persona, Persona


def _load_mode() -> str:
    """Return the ``COO_MODE`` value, defaulting to ``hub``.

    The environment variable is stripped of surrounding whitespace and
    lower‑cased to make the check case‑insensitive. If the variable is not
    set, ``"hub"`` is returned.
    """
    return os.getenv("COO_MODE", "hub").strip().lower()


def load_persona() -> Persona:
    """Load the persona appropriate to the current ``COO_MODE``.

    The function maps ``hub`` → ``alfred-coo-a`` and ``endpoint`` →
    ``autonomous-build-a`` (the orchestrator persona) according to the
    plan documentation for ticket F07. If a new mode is added later it can
    be extended here without touching the rest of the codebase.
    """
    mode = _load_mode()
    mapping = {
        "hub": "alfred-coo-a",
        "endpoint": "autonomous-build-a",
    }
    persona_name: Optional[str] = mapping.get(mode)
    if persona_name is None:
        # Unknown mode – fall back to hub and log a warning for ops.
        # Logging is deferred to the caller to avoid importing the
        # ``log`` module during import time.
        persona_name = "alfred-coo-a"
    return get_persona(persona_name)
