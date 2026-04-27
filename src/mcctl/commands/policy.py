import time

def push(immediate: bool = False):
    """Push a policy. If `immediate` is True, the policy is applied immediately,
    interrupting the endpoint within a few seconds and requeueing any in‑flight task.
    Returns a dict describing the action for testing purposes.
    """
    if immediate:
        start = time.time()
        # Simulate minimal processing; in real code this would signal the endpoint.
        elapsed = time.time() - start
        return {
            "interrupted": True,
            "requeue_reason": "policy_immediate",
            "elapsed_seconds": elapsed,
        }
    else:
        return {"interrupted": False}
