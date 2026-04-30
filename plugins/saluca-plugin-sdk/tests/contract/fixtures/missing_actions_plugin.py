from saluca_plugin_sdk import SalucaPlugin, DispatchResult

class MissingActionsPlugin(SalucaPlugin):
    direction = "outbound"
    # No outbound_actions attribute defined

    def dispatch_outbound(self, action: str, payload: dict) -> DispatchResult:
        return DispatchResult(output={})
