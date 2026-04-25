import subprocess
import sys

def test_mcctl_token_create(capsys):
    # Run the mcctl CLI with token create arguments
    result = subprocess.run([sys.executable, '-m', 'src.mcctl.__main__', 'token', 'create', '--site', 'acme-sfo', '--ttl', '15m'], capture_output=True, text=True)
    assert result.returncode == 0
    output = result.stdout.strip()
    assert output.startswith('dummy-')
    assert 'acme-sfo' in output
    assert '15m' in output
