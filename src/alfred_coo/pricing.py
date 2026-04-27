import yaml
from pathlib import Path
from typing import Dict, Any

def load_pricing() -> Dict[str, Any]:
    """Load the model pricing configuration from ``configs/model_pricing.yaml``.

    Returns a dictionary mapping provider names to their pricing details.
    """
    config_path = Path(__file__).resolve().parents[1] / "configs" / "model_pricing.yaml"
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data

# expose default instance for convenience
pricing = load_pricing()
