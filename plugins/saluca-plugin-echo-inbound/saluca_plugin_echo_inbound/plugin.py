from saluca_plugin_sdk import SalucaPlugin, DispatchResult

class EchoInboundPlugin(SalucaPlugin):
    """Inbound plugin that echoes the received task unchanged."""

    direction = "inbound"
    name = "echo_inbound"
    version = "0.1.0"

    def dispatch_inbound(self, task):
        # Simply return the task as output with status ok
        return DispatchResult(status="ok", output=task)
