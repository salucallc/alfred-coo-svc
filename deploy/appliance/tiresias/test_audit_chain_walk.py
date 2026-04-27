import pytest

def get_audit_chain():
    """
    Placeholder: fetch audit chain rows from the Tiresias audit service.
    In CI, this would query the service API.
    Returns a list of dicts with 'prev_hash' and 'hash' keys.
    """
    # Simulated empty chain for now
    return []

def check_chain_integrity(chain):
    """Returns number of breaks in the prev_hash links."""
    breaks = 0
    for i in range(1, len(chain)):
        if chain[i]["prev_hash"] != chain[i-1]["hash"]:
            breaks += 1
    return breaks

def test_audit_chain_walk():
    chain = get_audit_chain()
    assert check_chain_integrity(chain) == 0, "audit chain has hash-link breaks"
