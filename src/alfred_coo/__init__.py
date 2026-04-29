"""Headless Alfred COO daemon with consumer key management."""

import os
import sys
import hashlib
import base64
import warnings
from dataclasses import dataclass
from typing import Dict, Optional

# Module‑level state – persists for the lifetime of the process
_consumer_keys: Dict[str, str] = {}
_initialized: bool = False

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _generate_key(name: str) -> str:
    """Generate a deterministic secret for *name*.

    The secret is a hash of random 32‑byte material combined with the consumer
    name.  The resulting base64 string is used as the *key*; it never touches the
    filesystem.
    """
    rand = os.urandom(32)
    digest = hashlib.sha512(rand + name.encode()).digest()
    # Use URL‑safe base64 without padding – suitable for inclusion in URLs.
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def mint_all() -> Dict[str, str]:
    """Mint the four required consumer keys.

    The function is idempotent – subsequent calls are a no‑op after the first
    successful boot.  It returns the mapping ``{name: key}`` for the four
    consumers.
    """
    global _initialized
    if _initialized:
        return _consumer_keys.copy()

    for name in ["github", "slack", "linear", "notion"]:
        _consumer_keys[name] = _generate_key(name)
    _initialized = True
    return _consumer_keys.copy()


def reload_keys() -> None:
    """Reload keys – for this in‑memory implementation it simply ensures the
    boot process has been executed.  It is kept for API compatibility with the
    broader system.
    """
    # No persistent storage – nothing to reload.  Ensure minting happened.
    if not _initialized:
        mint_all()


@dataclass(frozen=True)
class ConsumerIdentity:
    """Simple identity object returned by :func:`verify` when a token is known.

    Attributes
    ----------
    name: str
        The consumer name (e.g. ``"github"``).
    token: str
        The full secret string associated with the consumer.
    """

    name: str
    token: str


def verify(token: str) -> Optional[ConsumerIdentity]:
    """Validate *token* against the minted consumer keys.

    Returns ``None`` if the token is unknown; otherwise returns a populated
    :class:`ConsumerIdentity` instance.
    """
    for name, key in _consumer_keys.items():
        if token == key:
            return ConsumerIdentity(name=name, token=key)
    return None


def check_file_permissions(path: str) -> bool:
    """Verify that *path* has mode ``0o600`` on POSIX systems.

    On Windows the check is skipped and a warning is emitted.
    Returns ``True`` when the permission is correct, ``False`` otherwise.
    """
    if sys.platform.startswith("win"):
        warnings.warn("Permission check skipped on Windows")
        return True
    try:
        mode = os.stat(path).st_mode & 0o777
        return mode == 0o600
    except FileNotFoundError:
        return False

# Initialise on import – first boot semantics.
mint_all()
