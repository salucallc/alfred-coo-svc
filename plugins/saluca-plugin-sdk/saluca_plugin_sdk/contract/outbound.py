from saluca_plugin_sdk import SalucaPlugin, DispatchResult
from typing import Any, Dict

def register(plugin: SalucaPlugin) -> None:
    """Register the outbound contract with the plugin system."""
    assert hasattr(plugin, "outbound_actions"), "Plugin missing outbound_actions"

def discover(plugin: SalucaPlugin) -> None:
    """Discover contract capabilities."""
    # Placeholder for discovery logic.
    pass

def lifecycle(plugin: SalucaPlugin) -> None:
    """Validate lifecycle behavior for outbound dispatch."""
    # Placeholder for lifecycle checks.
    pass

def dispatch_outbound(plugin: SalucaPlugin, action: str, payload: Dict[str, Any]) -> DispatchResult:
    """Dispatch an outbound action using the plugin's method."""
    return plugin.dispatch_outbound(action, payload)
