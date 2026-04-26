import yaml
from pathlib import Path

def load(path: str = "configs/model_pricing.yaml"):
    """Load the model pricing YAML and return as dict."""
    with Path(path).open() as f:
        return yaml.safe_load(f)

__all__ = ["load"]
