'''persona_loader.py

# Auto-generated for SAL-2615 (F07)
# Implements COO_MODE selection between hub and endpoint personas.
# Reference: https://raw.githubusercontent.com/salucallc/alfred-coo-svc/main/plans/v1-ga/C_fleet_mode_endpoint.md

import os
import yaml
import logging

logger = logging.getLogger(__name__)

def load_persona():
    """Load persona configuration based on the COO_MODE environment variable.

    Returns a dictionary representing the persona configuration. For `hub` mode
    an empty dict is returned (default behaviour). For `endpoint` mode the function
    attempts to read a `persona.yaml` file from a conventional location. If the file
    cannot be read, an empty dict is returned and a warning is logged.
    """
    mode = os.getenv("COO_MODE", "hub").lower()
    if mode == "endpoint":
        # Expected location for endpoint persona configuration.
        persona_path = "/etc/alfred-endpoint/persona.yaml"
        try:
            with open(persona_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                logger.info("Loaded endpoint persona from %s", persona_path)
                return data
        except FileNotFoundError:
            logger.warning("Persona file not found at %s; using empty persona", persona_path)
        except Exception as exc:
            logger.error("Failed to load persona file %s: %s", persona_path, exc)
        return {}
    # Default hub mode – no additional configuration required.
    logger.debug("COO_MODE=%s; using default hub persona", mode)
    return {}
"""
