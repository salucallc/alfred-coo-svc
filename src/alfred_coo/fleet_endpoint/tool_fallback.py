# Fallback implementations for degraded‑mode tools

"""Utility module providing concrete fallback behaviours for tools when the
fleet endpoint operates in degraded mode.

The functions below are simplistic stand‑ins used by the test suite. In a full
implementation they would interact with caches, queues, or the local filesystem
as dictated by the policy.
"""

from .degraded_mode import get_fallback_behaviour


def github_read(*args, **kwargs):
    """Fallback for ``mcp.github.read``.

    According to the degraded‑mode matrix the behaviour is ``cache_then_503``.
    """
    return "cached result, then HTTP 503"


def linear_write(*args, **kwargs):
    """Fallback for ``mcp.linear.write``.

    The matrix prescribes ``queue_and_drain``.
    """
    return "queued for later drain"


def local_fs_read(*args, **kwargs):
    """Fallback for ``local.fs.read``.

    This tool passes through unchanged in degraded mode.
    """
    return "passthrough read"


def fallback_for(tool_name: str):
    """Dispatch to the appropriate fallback implementation based on *tool_name*.
    """
    behaviour = get_fallback_behaviour(tool_name)
    if tool_name == "mcp.github.read":
        return github_read()
    if tool_name == "mcp.linear.write":
        return linear_write()
    if tool_name == "local.fs.read":
        return local_fs_read()
    return "default passthrough"
