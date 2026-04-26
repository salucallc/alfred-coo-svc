import unittest
from src.mcctl.commands.token import create_token

class TestMcctlTokenCreate(unittest.TestCase):
    def test_create_token_contains_site_and_ttl(self):
        site = 'acme-sfo'
        ttl = '15m'
        token = create_token(site, ttl)
        self.assertIn(site.upper(), token)
        self.assertIn(ttl, token)
        self.assertTrue(token.startswith('TOKEN-'))

if __name__ == '__main__':
    unittest.main()
