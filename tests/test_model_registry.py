"""Sub #62 — model registry hot-swap + reliability guardrails.

Tests cover the five behavioural contracts from the PR spec:

1. Loader returns the primary for a role.
2. Loader falls through the fallback chain on increasing attempt_n.
3. Schema-invalid registry falls back to the cached version (does NOT crash).
4. 3 consecutive hard-timeouts on a role auto-rolls to stable_baseline.
5. Registry mtime change triggers a re-read on the next pick.

The tests live in their own file so the registry module can be exercised
independently of the orchestrator's wave loop. Each test wipes the
module-level cache via `_reset_for_tests()` to guarantee isolation.
"""

from __future__ import annotations

import os
import time

import pytest

from alfred_coo.autonomous_build import model_registry as mr


# ── Fixtures ───────────────────────────────────────────────────────────────


_VALID_REGISTRY = """
schema_version: 1

models:
  qwen3-coder:480b-cloud:
    provider: ollama-cloud
    capabilities: [code-gen]
    status: active
  kimi-k2-thinking:cloud:
    provider: ollama-cloud
    capabilities: [reasoning]
    status: active
  gpt-oss:120b-cloud:
    provider: ollama-cloud
    capabilities: [tool-use]
    status: under_test

roles:
  builder:
    primary: "qwen3-coder:480b-cloud"
    fallback_chain: ["kimi-k2-thinking:cloud", "gpt-oss:120b-cloud"]
    last_resort: "gpt-oss:120b-cloud"
  qa:
    primary: "kimi-k2-thinking:cloud"
    fallback_chain: ["qwen3-coder:480b-cloud"]
    last_resort: "gpt-oss:120b-cloud"

stable_baseline:
  builder: "gpt-oss:120b-cloud"
  qa: "gpt-oss:120b-cloud"
"""


_VALID_REGISTRY_SWAPPED_BUILDER = """
schema_version: 1

models:
  qwen3-coder:480b-cloud:
    provider: ollama-cloud
    capabilities: [code-gen]
    status: active
  kimi-k2-thinking:cloud:
    provider: ollama-cloud
    capabilities: [reasoning]
    status: active
  gpt-oss:120b-cloud:
    provider: ollama-cloud
    capabilities: [tool-use]
    status: under_test

roles:
  builder:
    primary: "kimi-k2-thinking:cloud"   # swapped from qwen3-coder
    fallback_chain: ["qwen3-coder:480b-cloud"]
    last_resort: "gpt-oss:120b-cloud"
  qa:
    primary: "kimi-k2-thinking:cloud"
    fallback_chain: ["qwen3-coder:480b-cloud"]
    last_resort: "gpt-oss:120b-cloud"

stable_baseline:
  builder: "gpt-oss:120b-cloud"
  qa: "gpt-oss:120b-cloud"
"""


_INVALID_REGISTRY_BAD_YAML = """
schema_version: 1
roles:
  builder:
    primary: "qwen3-coder:480b-cloud
    fallback_chain: ["a"]
"""


_INVALID_REGISTRY_MISSING_KEYS = """
schema_version: 1
# missing models, roles, stable_baseline -- schema fails
"""


@pytest.fixture
def registry_path(tmp_path, monkeypatch):
    """Write a fresh valid registry to a temp path and point the loader at it."""
    p = tmp_path / "registry.yaml"
    p.write_text(_VALID_REGISTRY, encoding="utf-8")
    monkeypatch.setenv("MODEL_REGISTRY_PATH", str(p))
    mr._reset_for_tests()
    yield p
    mr._reset_for_tests()


# ── Test 1: primary for a role ─────────────────────────────────────────────


def test_registry_loader_returns_primary_for_role(registry_path):
    assert mr._pick_model_for_role("builder", attempt_n=0) == "qwen3-coder:480b-cloud"
    assert mr._pick_model_for_role("qa", attempt_n=0) == "kimi-k2-thinking:cloud"


# ── Test 2: fallback chain walks on attempts ───────────────────────────────


def test_registry_loader_falls_through_chain_on_attempts(registry_path):
    # builder: primary, chain[0], chain[1], past-chain (=> last_resort)
    assert mr._pick_model_for_role("builder", attempt_n=0) == "qwen3-coder:480b-cloud"
    assert mr._pick_model_for_role("builder", attempt_n=1) == "kimi-k2-thinking:cloud"
    assert mr._pick_model_for_role("builder", attempt_n=2) == "gpt-oss:120b-cloud"
    # past-the-chain returns last_resort
    assert mr._pick_model_for_role("builder", attempt_n=3) == "gpt-oss:120b-cloud"
    assert mr._pick_model_for_role("builder", attempt_n=99) == "gpt-oss:120b-cloud"


# ── Test 3: schema-invalid falls back to cached version (NO crash) ────────


def test_registry_loader_invalid_schema_falls_back_to_cached(registry_path):
    # First load: valid registry -> picks qwen3-coder.
    assert mr._pick_model_for_role("builder", attempt_n=0) == "qwen3-coder:480b-cloud"

    # Corrupt the file.
    registry_path.write_text(_INVALID_REGISTRY_BAD_YAML, encoding="utf-8")
    # Force mtime advance so the loader re-stats and tries to re-read.
    new_mtime = time.time() + 5
    os.utime(registry_path, (new_mtime, new_mtime))

    # Schema validation must fail silently, daemon continues on cache.
    pick = mr._pick_model_for_role("builder", attempt_n=0)
    assert pick == "qwen3-coder:480b-cloud", (
        f"expected cached pick on schema failure, got {pick}"
    )

    # Try with the missing-keys flavour for completeness.
    registry_path.write_text(_INVALID_REGISTRY_MISSING_KEYS, encoding="utf-8")
    new_mtime += 5
    os.utime(registry_path, (new_mtime, new_mtime))
    pick2 = mr._pick_model_for_role("builder", attempt_n=0)
    assert pick2 == "qwen3-coder:480b-cloud"


# ── Test 4: 3 consecutive hard-timeouts trip auto-rollback ─────────────────


def test_auto_rollback_after_3_consecutive_hard_timeouts(registry_path):
    # Sanity baseline: primary is qwen3-coder.
    assert mr._pick_model_for_role("builder", attempt_n=0) == "qwen3-coder:480b-cloud"

    # First two timeouts: counter increments, no rollback yet.
    crossed1 = mr.record_hard_timeout("builder")
    assert crossed1 is False
    assert mr.is_role_rolled_back("builder") is False
    assert mr._pick_model_for_role("builder", attempt_n=0) == "qwen3-coder:480b-cloud"

    crossed2 = mr.record_hard_timeout("builder")
    assert crossed2 is False
    assert mr.is_role_rolled_back("builder") is False
    assert mr._pick_model_for_role("builder", attempt_n=0) == "qwen3-coder:480b-cloud"

    # Third timeout trips the breaker.
    crossed3 = mr.record_hard_timeout("builder")
    assert crossed3 is True
    assert mr.is_role_rolled_back("builder") is True
    # Subsequent picks return stable_baseline regardless of attempt_n.
    assert mr._pick_model_for_role("builder", attempt_n=0) == "gpt-oss:120b-cloud"
    assert mr._pick_model_for_role("builder", attempt_n=1) == "gpt-oss:120b-cloud"
    assert mr._pick_model_for_role("builder", attempt_n=99) == "gpt-oss:120b-cloud"

    # Other roles untouched.
    assert mr._pick_model_for_role("qa", attempt_n=0) == "kimi-k2-thinking:cloud"

    # Idempotency: 4th timeout doesn't re-cross.
    crossed4 = mr.record_hard_timeout("builder")
    assert crossed4 is False
    assert mr.is_role_rolled_back("builder") is True


# ── Test 5: mtime change triggers reload + clears auto-rollback ────────────


def test_registry_mtime_change_triggers_reload(registry_path):
    # First dispatch: original registry, builder primary = qwen3-coder.
    pick_v1 = mr._pick_model_for_role("builder", attempt_n=0)
    assert pick_v1 == "qwen3-coder:480b-cloud"

    # Trip the auto-rollback so we can verify mtime change clears it as a
    # side-effect (operator intent semantics).
    mr.record_hard_timeout("builder")
    mr.record_hard_timeout("builder")
    mr.record_hard_timeout("builder")
    assert mr.is_role_rolled_back("builder") is True

    # Edit the file: swap builder primary to kimi-k2-thinking, advance mtime.
    registry_path.write_text(_VALID_REGISTRY_SWAPPED_BUILDER, encoding="utf-8")
    new_mtime = time.time() + 10
    os.utime(registry_path, (new_mtime, new_mtime))

    # Next pick should:
    #   (a) reload the registry on mtime change, AND
    #   (b) clear the auto-rollback (operator intervention semantics).
    pick_v2 = mr._pick_model_for_role("builder", attempt_n=0)
    assert pick_v2 == "kimi-k2-thinking:cloud", (
        f"expected swapped primary after mtime change, got {pick_v2}"
    )
    assert mr.is_role_rolled_back("builder") is False


# ── Bonus coverage: edge cases and contract guarantees ─────────────────────


def test_unknown_role_returns_none(registry_path):
    """An unmapped role should return None so the caller falls back to legacy."""
    assert mr._pick_model_for_role("nonexistent-role", attempt_n=0) is None


def test_registry_missing_returns_none(monkeypatch, tmp_path):
    """No file on disk, no env, no canonical path => None (caller falls back)."""
    monkeypatch.setenv("MODEL_REGISTRY_PATH", str(tmp_path / "definitely-not-here.yaml"))
    # Patch the canonical path list to also point at non-existent paths.
    monkeypatch.setattr(
        mr, "_DEFAULT_REGISTRY_PATHS",
        [str(tmp_path / "x.yaml"), str(tmp_path / "y.yaml")],
    )
    mr._reset_for_tests()
    assert mr._load_model_registry() is None
    assert mr._pick_model_for_role("builder", attempt_n=0) is None


def test_record_success_resets_counter_but_not_rollback(registry_path):
    """record_success() resets the streak counter but leaves auto_rollback set
    until next mtime change — by design, prevents fluky-but-recovering primary
    from re-promoting without operator review.
    """
    mr.record_hard_timeout("builder")
    mr.record_hard_timeout("builder")
    mr.record_hard_timeout("builder")
    assert mr.is_role_rolled_back("builder") is True

    mr.record_success("builder")
    # Counter reset, but rollback still active.
    assert mr.is_role_rolled_back("builder") is True
    # Reset means next 3 timeouts could trip it again (it's already tripped,
    # but the streak counter is at 0).
    assert mr._cache.consecutive_hard_timeouts.get("builder", 0) == 0
