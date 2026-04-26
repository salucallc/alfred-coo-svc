def test_token_create_output(capsys):
    import sys
    from mcctl.commands import token
    sys.argv = ['mcctl', 'token', '--site', 'acme-sfo', '--ttl', '15m']
    token.main()
    captured = capsys.readouterr()
    assert 'Generated token for site acme-sfo with ttl 15m' in captured.out
