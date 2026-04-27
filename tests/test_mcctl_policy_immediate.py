import json
from unittest import mock
import click
from click.testing import CliRunner

from src.mcctl.commands.policy import policy

def test_policy_push_immediate_flag_calls_client():
    runner = CliRunner()
    with mock.patch("src.mcctl.client.MCClient") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.post_json.return_value.status_code = 200

        result = runner.invoke(policy, ["push", "--immediate"])
        assert result.exit_code == 0
        mock_instance.post_json.assert_called_once()
        args, kwargs = mock_instance.post_json.call_args
        assert args[0] == "/v1/fleet/policy/push"
        payload = kwargs.get("json")
        assert payload == {"immediate": True}
        assert "Policy push successful" in result.output

def test_policy_push_without_immediate_flag():
    runner = CliRunner()
    with mock.patch("src.mcctl.client.MCClient") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.post_json.return_value.status_code = 200

        result = runner.invoke(policy, ["push"])
        assert result.exit_code == 0
        mock_instance.post_json.assert_called_once()
        payload = mock_instance.post_json.call_args[1].get("json")
        assert payload == {"immediate": False}
        assert "Policy push successful" in result.output
