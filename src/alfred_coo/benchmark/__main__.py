"""Package entry so ``python -m alfred_coo.benchmark run ...`` works."""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
