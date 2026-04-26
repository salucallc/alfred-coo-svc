import argparse
from .commands import token

def main():
    parser = argparse.ArgumentParser(prog='mcctl')
    subparsers = parser.add_subparsers(dest='command')
    token_parser = subparsers.add_parser('token', help='Token commands')
    token_subparsers = token_parser.add_subparsers(dest='action')
    create_parser = token_subparsers.add_parser('create', help='Create a token')
    create_parser.add_argument('--site', required=True, help='Site code')
    create_parser.add_argument('--ttl', required=True, help='Time to live, e.g., 15m')
    args = parser.parse_args()
    if args.command == 'token' and args.action == 'create':
        token_str = token.create_token(args.site, args.ttl)
        print(token_str)

if __name__ == '__main__':
    main()
