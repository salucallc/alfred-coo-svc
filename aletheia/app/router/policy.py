"""Model router for Aletheia.

Maps (action_class, risk_tier) to a verifier model ID.
Refuses when the verifier model would equal the generator model.
"""

from typing import Tuple

# Routing table as per ALT-04 specification
_ROUTING_TABLE = {
    ("pr_review", "high"): "qwen3-coder:480b-cloud",
    ("pr_review", "low"): "hf:openai/gpt-oss-120b:fastest",
    ("slack_send", "med"): "qwen3-coder:480b-cloud",
    ("slack_send", "low"): "hf:openai/gpt-oss-120b:fastest",
    ("linear_close_issue", "med"): "qwen3-coder:480b-cloud",
    ("notion_write", "low"): "hf:openai/gpt-oss-120b:fastest",
    ("mesh_task_complete", "high"): "qwen3-coder:480b-cloud",
    ("mesh_task_complete", "low"): "hf:openai/gpt-oss-120b:fastest",
    ("generator", "deepseek"): "qwen3-coder:480b-cloud",  # forced case
}

def get_verifier_model(action_class: str, risk_tier: str, generator_model: str) -> str:
    """Return the verifier model for the given action.

    Raises:
        ValueError: If the routing table has no entry for the pair, or if the
            chosen verifier model equals the provided generator_model.
    """
    key: Tuple[str, str] = (action_class, risk_tier)
    if key not in _ROUTING_TABLE:
        raise ValueError(f"No routing entry for {key}")
    verifier = _ROUTING_TABLE[key]
    if verifier == generator_model:
        raise ValueError("Verifier model must differ from generator model")
    return verifier
