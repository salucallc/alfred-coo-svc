import pytest
from aletheia.app.router.policy import get_verifier_model

# Expected routing table entries (action_class, risk_tier, expected verifier)
_ROUTING_CASES = [
    ("pr_review", "high", "qwen3-coder:480b-cloud"),
    ("pr_review", "low", "hf:openai/gpt-oss-120b:fastest"),
    ("slack_send", "med", "qwen3-coder:480b-cloud"),
    ("slack_send", "low", "hf:openai/gpt-oss-120b:fastest"),
    ("linear_close_issue", "med", "qwen3-coder:480b-cloud"),
    ("notion_write", "low", "hf:openai/gpt-oss-120b:fastest"),
    ("mesh_task_complete", "high", "qwen3-coder:480b-cloud"),
    ("mesh_task_complete", "low", "hf:openai/gpt-oss-120b:fastest"),
    # forced routing for deepseek generator
    ("generator_deepseek", "any", "qwen3-coder:480b-cloud"),
]

@pytest.mark.parametrize("action_class,risk_tier,expected", _ROUTING_CASES")
def test_routing_success(action_class, risk_tier, expected):
    # Use a dummy generator model that differs from the expected verifier
    dummy_generator = "different-model"
    assert get_verifier_model(action_class, risk_tier, dummy_generator) == expected

def test_router_refuses_same_model():
    # When generator model equals the verifier model, a ValueError is raised
    with pytest.raises(ValueError):
        get_verifier_model("pr_review", "high", "qwen3-coder:480b-cloud")
