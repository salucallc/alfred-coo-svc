from typing import Any, Dict

def dispatch_inbound(task: Dict[str, Any]) -> Any:
    """Placeholder for inbound dispatch handling."""
    return {"status": "inbound_handled", "task": task}

def dispatch_outbound(task: Dict[str, Any]) -> Any:
    """Placeholder for outbound dispatch handling."""
    return {"status": "outbound_handled", "task": task}

def route_task(direction: str, task: Dict[str, Any]) -> Any:
    """
    Route a task based on the agent's direction.

    Args:
        direction: One of "inbound", "outbound", or "bidirectional".
        task: Arbitrary task payload.

    Returns:
        The result of the appropriate dispatch function.

    Raises:
        ValueError: If the direction is unsupported.
    """
    if direction == "inbound":
        return dispatch_inbound(task)
    if direction == "outbound":
        return dispatch_outbound(task)
    if direction == "bidirectional":
        return dispatch_inbound(task)
    raise ValueError("Invalid direction")
