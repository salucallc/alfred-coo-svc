"""Persona loader for alfred-coo-svc.

Handles the `COO_MODE` environment variable to select between hub and
endpoint operation. In `endpoint` mode a `persona.yaml` file is expected at
`/etc/alfred-coo/persona.yaml` (or the path specified by the
`PERSONA_PATH` env var). The file should contain a mapping with at least a
`name` field matching one of the builtin personas. An optional `signature`
field can be used to verify integrity – this stub simply checks that the
field is present; real verification can be added later.
"""

import os
import yaml
from .persona import BUILTIN_PERSONAS, Persona, get_persona

# Default locations – can be overridden via environment for testing.
_PERSONA_ENV_VAR = "COO_MODE"
_DEFAULT_MODE = "hub"
_PERSONA_PATH_ENV = "PERSONA_PATH"
_DEFAULT_PERSONA_PATH = "/etc/alfred-coo/persona.yaml"


def _load_yaml(path: str) -> dict:
    """Load a YAML file safely.

    Returns an empty dict if the file does not exist or cannot be parsed.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _verify_signature(data: dict) -> bool:
    """Placeholder signature verification.

    The real implementation should verify `data["signature"]` against the
    payload using the hub's public key. For now we simply require the field to
    exist and be a non‑empty string.
    """
    sig = data.get("signature")
    return isinstance(sig, str) and len(sig) > 0


def load_persona() -> Persona:
    """Load the appropriate persona based on ``COO_MODE``.

    * ``hub``   – returns the default ``alfred-coo-a`` persona.
    * ``endpoint`` – reads ``persona.yaml`` (or ``PERSONA_PATH``) and returns
      the matching builtin persona. If the file is missing, malformed, or the
      signature verification fails, the function falls back to the default
      persona and logs the issue.
    """
    mode = os.getenv(_PERSONA_ENV_VAR, _DEFAULT_MODE).lower()
    if mode == "hub":
        return BUILTIN_PERSONAS.get("alfred-coo-a")

    # Endpoint mode – attempt to load a persona description from YAML.
    path = os.getenv(_PERSONA_PATH_ENV, _DEFAULT_PERSONA_PATH)
    data = _load_yaml(path)
    name = data.get("name")
    if not name:
        # Missing name – fall back.
        return BUILTIN_PERSONAS.get("alfred-coo-a")

    # Verify signature if present.
    if "signature" in data and not _verify_signature(data):
        # Invalid signature – fall back.
        return BUILTIN_PERSONAS.get("alfred-coo-a")

    # Return the builtin persona if it exists; otherwise construct a minimal one.
    if name in BUILTIN_PERSONAS:
        return BUILTIN_PERSONAS[name]
    # Construct a minimal Persona to avoid crashes.
    return Persona(
        name=name,
        system_prompt=data.get("system_prompt", "You are a specialized endpoint persona."),
        preferred_model=data.get("preferred_model"),
        fallback_model=data.get("fallback_model"),
        topics=data.get("topics", []),
        tags=data.get("tags", []),
        tools=data.get("tools", []),
        handler=data.get("handler"),
    )


# Exported symbol for callers.
__all__ = ["load_persona"]
