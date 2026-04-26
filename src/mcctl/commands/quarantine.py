# mcctl unquarantine command implementation
"""
Command line helper to recover an endpoint from quarantine.

Usage example::

    mcctl endpoint unquarantine <endpoint_id>
"""

import argparse
from alfred_coo.fleet_endpoint import quarantine as qz


def build_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``unquarantine`` sub‑command.

    The ``mcctl`` entry point will call this to add the command to its CLI.
    """
    parser = subparsers.add_parser(
        "unquarantine",
        help="Recover an endpoint from quarantine",
    )
    parser.add_argument("endpoint_id", help="ID of the endpoint to recover")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    """Execute the command.

    It simply clears the quarantine flag for the supplied endpoint and prints a short message.
    """
    qz.unquarantine(args.endpoint_id)
    print(f"Endpoint {args.endpoint_id} recovered from quarantine.")
