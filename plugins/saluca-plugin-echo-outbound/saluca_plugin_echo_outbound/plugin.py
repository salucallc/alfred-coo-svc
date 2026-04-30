from saluca_plugin_sdk import SalucaPlugin, DispatchResult
import requests

class EchoOutboundPlugin(SalucaPlugin):
    """Simple outbound plugin that sends a POST request to a mock endpoint."""

    direction = "outbound"
    outbound_actions = ["echo.send"]

    def dispatch_outbound(self, action: str, payload: dict) -> DispatchResult:
        """Dispatch the given outbound action.

        Args:
            action: The action name, must be in outbound_actions.
            payload: JSON‑serializable payload to send.

        Returns:
            DispatchResult containing the response data.
        """
        if action not in self.outbound_actions:
            raise ValueError(f"Unsupported outbound action: {action}")

        endpoint = self.config.get("endpoint", "http://localhost:8000/echo")
        response = requests.post(endpoint, json=payload)
        response.raise_for_status()
        return DispatchResult(output=response.json())
