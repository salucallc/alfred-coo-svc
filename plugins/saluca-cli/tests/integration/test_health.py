import subprocess, pathlib

def test_cli_health_command():
    # Assuming the CLI entrypoint is `saluca-cli` and has a health subcommand
    result = subprocess.run(['saluca-cli', 'health'], capture_output=True, text=True)
    assert result.returncode == 0
    assert 'healthy' in result.stdout.lower()
