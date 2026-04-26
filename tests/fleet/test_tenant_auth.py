import sys
from argparse import ArgumentParser

# Import the token module
sys.path.append('src')
from mcctl.commands import token

def test_token_parser_includes_tenant_flag(monkeypatch):
    # Simulate command line arguments
    test_args = ['--site', 'acme', '--ttl', '15m', '--tenant', 'acme-corp']
    monkeypatch.setattr(sys, 'argv', ['prog'] + test_args)
    # Capture printed output
    captured = []
    def fake_print(msg):
        captured.append(msg)
    monkeypatch.setattr('builtins.print', fake_print)
    token.main()
    assert any('tenant=acme-corp' in msg for msg in captured)
