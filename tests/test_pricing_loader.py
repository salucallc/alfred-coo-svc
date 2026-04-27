"""Tests for the pricing loader.
"""

import pytest
from src.alfred_coo import pricing


def test_pricing_load_contains_free_tier():
    data = pricing.load()
    assert "openrouter" in data
    free = data["openrouter"].get("free")
    assert free is not None
    assert free["input_per_1k"] == 0.0
    assert free["output_per_1k"] == 0.0
