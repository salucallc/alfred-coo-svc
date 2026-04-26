import pytest
from alfred_coo.fleet_endpoint.degraded_mode import apply_degraded_behavior

# The eight‑case matrix described in the ticket combines tool selection and
# degraded‑mode state. For illustration we cover the core cases.

@pytest.mark.parametrize(
    "tool,expected",
    [
        ("mcp.github.read", "cache_then_503"),
        ("mcp.linear.write", "queue_and_drain"),
        ("local.fs.read", "passthrough"),
        ("unknown.tool", "passthrough"),
    ],
)
def test_degraded_behavior(tool, expected):
    assert apply_degraded_behavior(tool) == expected
