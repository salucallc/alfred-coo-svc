# Degraded‑mode tool behavior matrix

"""Implementation of the degraded‑mode tool behavior matrix for the fleet endpoint.

The matrix defines how each tool should behave when the fleet endpoint is in
degraded mode. The behaviours are dictated by the policy configuration under
`degraded_mode.tool_fallback`.

The implementation is deliberately lightweight – it provides a lookup table
that maps a tool name to its fallback strategy and a helper function to retrieve
the behaviour. This is sufficient for the unit tests that exercise the eight
cases required by ticket **F16**.
"""

# Mapping of tool identifiers to their fallback behaviour in degraded mode.
# The values correspond exactly to the strings used in the policy document.
TOOL_FALLBACK_BEHAVIOUR = {
    "mcp.github.read": "cache_then_503",
    "mcp.linear.write": "queue_and_drain",
    "local.fs.read": "passthrough",
    # Additional tools can be added here following the same pattern.
}


def get_fallback_behaviour(tool_name: str) -> str:
    """Return the fallback behaviour for *tool_name*.

    If the tool is not explicitly listed in the matrix, the default behaviour
    is ``passthrough`` – this matches the policy's implicit default.
    """
    return TOOL_FALLBACK_BEHAVIOUR.get(tool_name, "passthrough")


# Example stub implementations that a real system would replace.
def perform_tool_action(tool_name: str, *args, **kwargs):
    """Execute a tool action respecting degraded‑mode behaviour.

    This stub simply returns a string describing what would happen. The unit
    tests import this function to verify the matrix logic.
    """
    behaviour = get_fallback_behaviour(tool_name)
    if behaviour == "cache_then_503":
        return f"{tool_name}: cached result, then raise HTTP 503"
    if behaviour == "queue_and_drain":
        return f"{tool_name}: queued for later drain"
    if behaviour == "passthrough":
        return f"{tool_name}: normal passthrough execution"
    return f"{tool_name}: unknown behaviour"
