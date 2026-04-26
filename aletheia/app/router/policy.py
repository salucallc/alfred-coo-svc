from typing import Tuple

# Mapping of (action_class, risk_tier) to verifier model
_ROUTING_TABLE = {
    ("pr_review", "high"): "qwen3-coder:480b-cloud",
    ("pr_review", "low"): "hf:openai/gpt-oss-120b:fastest",
    ("slack_send", "med"): "qwen3-coder:480b-cloud",
    ("slack_send", "low"): "hf:openai/gpt-oss-120b:fastest",
    ("linear_close_issue", "med"): "qwen3-coder:480b-cloud",
    ("notion_write", "low"): "hf:openai/gpt-oss-120b:fastest",
    ("mesh_task_complete", "high"): "qwen3-coder:480b-cloud",
    ("mesh_task_complete", "low"): "hf:openai/gpt-oss-120b:fastest",
}

DEFAULT_MODEL = "hf:openai/gpt-oss-120b:fastest"

def get_verifier_model(action_class: str, risk_tier: str, generator_model: str) -> str:
    """Return the verifier model for given action and risk.
    Raises ValueError if the selected verifier equals the generator model.
    """
    model = _ROUTING_TABLE.get((action_class, risk_tier), DEFAULT_MODEL)
    if model == generator_model:
        raise ValueError("Verifier model must differ from generator model")
    return model
