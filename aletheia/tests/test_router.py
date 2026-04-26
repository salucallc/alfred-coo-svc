import pytest
from aletheia.app.router.policy import get_verifier_model

# 12 test rows covering the matrix defined in ALT-04
@pytest.mark.parametrize(
    "action_class, risk_tier, generator_model, expected",
    [
        ("pr_review", "high", "hf:openai/gpt-oss-120b:fastest", "qwen3-coder:480b-cloud"),
        ("pr_review", "low", "qwen3-coder:480b-cloud", "hf:openai/gpt-oss-120b:fastest"),
        ("slack_send", "med", "hf:openai/gpt-oss-120b:fastest", "qwen3-coder:480b-cloud"),
        ("slack_send", "low", "qwen3-coder:480b-cloud", "hf:openai/gpt-oss-120b:fastest"),
        ("linear_close_issue", "med", "hf:openai/gpt-oss-120b:fastest", "qwen3-coder:480b-cloud"),
        ("notion_write", "low", "qwen3-coder:480b-cloud", "hf:openai/gpt-oss-120b:fastest"),
        ("mesh_task_complete", "high", "hf:openai/gpt-oss-120b:fastest", "qwen3-coder:480b-cloud"),
        ("mesh_task_complete", "low", "qwen3-coder:480b-cloud", "hf:openai/gpt-oss-120b:fastest"),
        # Forced generator case
        ("generator", "deepseek", "qwen3-coder:480b-cloud", "qwen3-coder:480b-cloud"),
        # Additional representative combos
        ("pr_review", "high", "qwen3-coder:480b-cloud", "qwen3-coder:480b-cloud"),
        ("slack_send", "med", "qwen3-coder:480b-cloud", "qwen3-coder:480b-cloud"),
        ("mesh_task_complete", "high", "qwen3-coder:480b-cloud", "qwen3-coder:480b-cloud"),
    ],
)
def test_router(action_class, risk_tier, generator_model, expected):
    if generator_model == expected:
        # Should raise due to verifier == generator
        with pytest.raises(ValueError, match="Verifier model must differ"):
            get_verifier_model(action_class, risk_tier, generator_model)
    else:
        assert get_verifier_model(action_class, risk_tier, generator_model) == expected
