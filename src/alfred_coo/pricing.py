"""Pricing loader for model costs.

Loads the YAML file at `configs/model_pricing.yaml` and returns a
dictionary mapping providers to their pricing details.
"""

import os
import yaml
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "model_pricing.yaml"


def load() -> dict:
    """Load the model pricing YAML file.

    Returns:
        dict: The pricing configuration.
    """
    if not CONFIG_PATH.is_file():
        raise FileNotFoundError(f"Pricing config not found at {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}
