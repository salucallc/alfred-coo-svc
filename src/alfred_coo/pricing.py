import yaml
from pathlib import Path

def load() -> dict:
    """Load the model pricing configuration from the repository's config file."""
    config_path = Path(__file__).resolve().parent.parent / "configs" / "model_pricing.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
