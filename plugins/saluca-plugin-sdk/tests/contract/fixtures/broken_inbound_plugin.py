from saluca_plugin_sdk import SalucaPlugin, DispatchResult

class BrokenPlugin(SalucaPlugin):
    direction = "inbound"
    name = "broken_inbound"
    version = "0.1.0"

    def dispatch_inbound(self, task):
        # Return wrong type to trigger contract failure
        return "not a DispatchResult"
