import os
import yaml

def load() -> dict:
    """Load model pricing configuration.

    Returns:
        dict: Pricing data structured as a mapping.
    """
    config_path = os.path.join(os.path.dirname(__file__), "..", "configs", "model_pricing.yaml")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {}
    return data
