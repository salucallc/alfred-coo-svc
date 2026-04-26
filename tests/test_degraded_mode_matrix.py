# Tests for the degraded‑mode tool behaviour matrix (F16)

import unittest
from alfred_coo.fleet_endpoint.degraded_mode import get_fallback_behaviour, perform_tool_action
from alfred_coo.fleet_endpoint.tool_fallback import fallback_for

class TestDegradedModeMatrix(unittest.TestCase):
    def test_fallback_lookup(self):
        self.assertEqual(get_fallback_behaviour("mcp.github.read"), "cache_then_503")
        self.assertEqual(get_fallback_behaviour("mcp.linear.write"), "queue_and_drain")
        self.assertEqual(get_fallback_behaviour("local.fs.read"), "passthrough")
        # Default for unknown tool
        self.assertEqual(get_fallback_behaviour("unknown.tool"), "passthrough")

    def test_perform_tool_action(self):
        self.assertIn("cached result", perform_tool_action("mcp.github.read"))
        self.assertIn("queued", perform_tool_action("mcp.linear.write"))
        self.assertIn("passthrough", perform_tool_action("local.fs.read"))
        # Unknown tool falls back to default path
        self.assertIn("unknown", perform_tool_action("unknown.tool"))

    def test_fallback_dispatch(self):
        self.assertEqual(fallback_for("mcp.github.read"), "cached result, then HTTP 503")
        self.assertEqual(fallback_for("mcp.linear.write"), "queued for later drain")
        self.assertEqual(fallback_for("local.fs.read"), "passthrough read")
        self.assertEqual(fallback_for("other.tool"), "default passthrough")

if __name__ == "__main__":
    unittest.main()
