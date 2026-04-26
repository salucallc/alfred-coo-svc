import yaml
import os

def load() -> dict:
    """
    Load the model pricing configuration from the YAML file.

    Returns:
        dict: Parsed pricing configuration.
    """
    base_dir = os.path.dirname(__file__)
    # The pricing config resides in the top-level configs directory
    config_path = os.path.abspath(os.path.join(base_dir, "..", "..", "configs", "model_pricing.yaml"))
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)