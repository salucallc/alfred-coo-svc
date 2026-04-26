# Tool fallback implementations for degraded mode

from .degraded_mode import apply_degraded_behavior

def fallback_tool(tool_name: str, *args, **kwargs):
    """Execute the appropriate fallback for a tool when degraded mode is active.

    This stub returns the fallback behavior string; real implementation would
    invoke the corresponding fallback mechanism (cache, queue, etc.).
    """
    return apply_degraded_behavior(tool_name)
