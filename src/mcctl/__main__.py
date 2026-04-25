#!/usr/bin/env python3
"""Entry point for mcctl CLI.
"""
import argparse
from .commands import token

def main():
    parser = argparse.ArgumentParser(prog='mcctl')
    subparsers = parser.add_subparsers(dest='command')
    # token command
    token_parser = subparsers.add_parser('token', help='Token related commands')
    token_sub = token_parser.add_subparsers(dest='subcommand')
    create_parser = token_sub.add_parser('create', help='Create a one‑shot token')
    create_parser.add_argument('--site', required=True, help='Site code')
    create_parser.add_argument('--ttl', required=True, help='Time‑to‑live, e.g. 15m')
    args = parser.parse_args()
    if args.command == 'token' and args.subcommand == 'create':
        token.create_token(args.site, args.ttl)
    else:
        parser.print_help()

if __name__ == '__main__':
    main()
