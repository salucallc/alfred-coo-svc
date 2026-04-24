"""CLI entry point for the benchmark runner.

Usage::

    python -m alfred_coo.benchmark run --move M-MV-01 --model gpt-oss:120b-cloud
    python -m alfred_coo.benchmark run --all --model deepseek-v3.2:cloud

Plan M §5.1 scopes the ``score``, ``report``, and ``watch`` subcommands to
M-06/M-07; we intentionally ship only ``run`` here so M-01/02 stays minimal.
The ``run`` subcommand is enough to reproduce a v8-smoke failure against a
candidate model and is the piece that unblocks v8-smoke-d's next iteration.

Stdout is a single JSON document per invocation — easy for a later
``benchmark-svc`` (M-03) to wrap and publish, easy for a developer to eyeball.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any, Dict, List

from .runner import run_fixture
from .schema import Fixture, load_all_fixtures, load_fixture


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m alfred_coo.benchmark",
        description="Plan M benchmark runner (M-01/M-02 slice).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Run one fixture or all fixtures against a model.")
    group = run_p.add_mutually_exclusive_group(required=True)
    group.add_argument("--move", help="Move id (e.g. M-MV-01).")
    group.add_argument("--all", action="store_true", help="Run all fixtures.")
    run_p.add_argument("--model", required=True, help="Candidate model id.")
    run_p.add_argument("--n-samples", type=int, default=3, help="Samples per fixture (default 3).")
    run_p.add_argument("--gateway-url", default=None, help="Gateway base URL (else use env).")
    run_p.add_argument("--verbose", "-v", action="count", default=0)
    return p


async def _run(args: argparse.Namespace) -> int:
    fixtures: List[Fixture] = (
        load_all_fixtures() if args.all else [load_fixture(args.move)]
    )

    output: List[Dict[str, Any]] = []
    exit_code = 0
    for fixture in fixtures:
        result = await run_fixture(
            fixture,
            args.model,
            n_samples=args.n_samples,
            gateway_url=args.gateway_url,
        )
        output.append(result.to_dict())
        if not result.majority_passed:
            exit_code = 1

    json.dump({"results": output}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return exit_code


def main(argv: List[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")

    if args.cmd == "run":
        return asyncio.run(_run(args))
    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
