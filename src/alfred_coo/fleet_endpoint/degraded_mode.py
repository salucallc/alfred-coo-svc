# Degraded mode utilities for fleet endpoint

def apply_degraded_behavior(tool_name: str) -> str:
    """Return fallback behavior for a given tool according to the degraded‑mode policy.

    Args:
        tool_name: Identifier of the tool (e.g. ``mcp.github.read``).

    Returns:
        A string describing the fallback action.
    """
    fallback_map = {
        "mcp.github.read": "cache_then_503",
        "mcp.linear.write": "queue_and_drain",
        "local.fs.read": "passthrough",
    }
    return fallback_map.get(tool_name, "passthrough")
