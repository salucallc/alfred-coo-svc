"""Alfred Coo - The personal assistant for data professionals."""

import asyncio

from .main import main


def main_sync() -> None:
    """Synchronous entry point for the Alfred Coo CLI."""
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
