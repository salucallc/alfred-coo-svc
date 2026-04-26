import yaml
from pathlib import Path

def load() -> dict:
    config_path = Path(__file__).resolve().parents[1] / "configs" / "model_pricing.yaml"
    with open(config_path, "r") as f:
        data = yaml.safe_load(f)

    result = {}
    for provider, details in data.get("providers", {}).items():
        if isinstance(details, dict) and any(isinstance(v, dict) for v in details.values()):
            for model, pricing in details.items():
                key = f"{provider}/{model}"
                result[key] = pricing
        else:
            result[provider] = details
    return result
