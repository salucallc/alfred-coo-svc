from typing import Tuple

# Mapping of (action_class, risk_tier) to verifier model identifier
_ROUTER_TABLE = {
    ("pr_review", "high"): "qwen3-coder:480b-cloud",
    ("pr_review", "low"): "hf:openai/gpt-oss-120b:fastest",
    ("slack_send", "med"): "qwen3-coder:480b-cloud",
    ("slack_send", "low"): "hf:openai/gpt-oss-120b:fastest",
    ("linear_close_issue", "med"): "qwen3-coder:480b-cloud",
    ("notion_write", "low"): "hf:openai/gpt-oss-120b:fastest",
    ("mesh_task_complete", "high"): "qwen3-coder:480b-cloud",
    ("mesh_task_complete", "low"): "hf:openai/gpt-oss-120b:fastest",
    ("generator_deepseek", "any"): "qwen3-coder:480b-cloud",
}

def get_verifier_model(action_class: str, risk_tier: str, generator_model: str) -> str:
    """Return the verifier model for a given action class and risk tier.

    Args:
        action_class: The high‑level classification of the mutation (e.g. ``pr_review``).
        risk_tier: One of ``high``, ``med`` or ``low`` indicating the stakes.
        generator_model: The model used for the generator side of the flow.

    Returns:
        The identifier of the verifier model to use.

    Raises:
        KeyError: If the (action_class, risk_tier) combination is not recognised.
        ValueError: If the generator model would be the same as the verifier model.
    """
    key: Tuple[str, str] = (action_class, risk_tier)
    # Fallback for generator-specific forced routing
    if action_class.startswith("generator_"):
        verifier = _ROUTER_TABLE.get(("generator_deepseek", "any"))
    else:
        verifier = _ROUTER_TABLE[key]
    if generator_model == verifier:
        raise ValueError("router refuses when generator_model == verifier_model")
    return verifier
