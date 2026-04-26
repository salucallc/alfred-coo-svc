import pytest
from aletheia.app.router.policy import get_verifier_model, DEFAULT_MODEL

@pytest.mark.parametrize(
    "action,risk,gen,expected",
    [
        ("pr_review", "high", "some-other", "qwen3-coder:480b-cloud"),
        ("pr_review", "low", "some-other", "hf:openai/gpt-oss-120b:fastest"),
        ("slack_send", "med", "some-other", "qwen3-coder:480b-cloud"),
        ("slack_send", "low", "some-other", "hf:openai/gpt-oss-120b:fastest"),
        ("linear_close_issue", "med", "some-other", "qwen3-coder:480b-cloud"),
        ("notion_write", "low", "some-other", "hf:openai/gpt-oss-120b:fastest"),
        ("mesh_task_complete", "high", "some-other", "qwen3-coder:480b-cloud"),
        ("mesh_task_complete", "low", "some-other", "hf:openai/gpt-oss-120b:fastest"),
        # Conflict cases – expect exception
        ("pr_review", "high", "qwen3-coder:480b-cloud", None),
        ("pr_review", "low", "hf:openai/gpt-oss-120b:fastest", None),
        ("slack_send", "med", "qwen3-coder:480b-cloud", None),
        ("slack_send", "low", "hf:openai/gpt-oss-120b:fastest", None),
    ],
)
def test_routing(action, risk, gen, expected):
    if expected is None:
        with pytest.raises(ValueError):
            get_verifier_model(action, risk, gen)
    else:
        assert get_verifier_model(action, risk, gen) == expected
