"""Behavioural smoke for sub #62 — registry hot-swap + schema-rejection.

Two smokes per the spec:

(d) End-to-end hot-swap: edit registry.yaml to swap builder primary from
    qwen3-coder:480b-cloud to kimi-k2-thinking:cloud, run a synthetic
    dispatch through the loader, assert the new model is picked WITHOUT
    daemon restart.

(e) Schema rejection: corrupt the YAML, run another dispatch, assert it
    falls back to cached version (NOT crashes).

Designed to run standalone: `python scripts/smoke_model_registry.py`
without arguments. Output is plain stdout for caller capture.
"""

from __future__ import annotations

import os
import sys
import time
import textwrap
from pathlib import Path

# Ensure src/ is importable when invoked from the repo root.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alfred_coo.autonomous_build import model_registry as mr  # noqa: E402


GOOD_V1 = textwrap.dedent("""
    schema_version: 1
    models:
      qwen3-coder:480b-cloud: {provider: ollama-cloud, capabilities: [code], status: active}
      kimi-k2-thinking:cloud: {provider: ollama-cloud, capabilities: [reason], status: active}
      gpt-oss:120b-cloud:    {provider: ollama-cloud, capabilities: [tool], status: under_test}
    roles:
      builder:
        primary: "qwen3-coder:480b-cloud"
        fallback_chain: ["kimi-k2-thinking:cloud"]
        last_resort: "gpt-oss:120b-cloud"
    stable_baseline:
      builder: "gpt-oss:120b-cloud"
""").strip()

GOOD_V2_SWAPPED = textwrap.dedent("""
    schema_version: 1
    models:
      qwen3-coder:480b-cloud: {provider: ollama-cloud, capabilities: [code], status: active}
      kimi-k2-thinking:cloud: {provider: ollama-cloud, capabilities: [reason], status: active}
      gpt-oss:120b-cloud:    {provider: ollama-cloud, capabilities: [tool], status: under_test}
    roles:
      builder:
        primary: "kimi-k2-thinking:cloud"
        fallback_chain: ["qwen3-coder:480b-cloud"]
        last_resort: "gpt-oss:120b-cloud"
    stable_baseline:
      builder: "gpt-oss:120b-cloud"
""").strip()

CORRUPT_YAML = textwrap.dedent("""
    schema_version: 1
    roles:
      builder:
        primary: "qwen3-coder:480b-cloud
        fallback_chain: [
""").strip()


def main() -> int:
    workdir = Path(os.environ.get("SMOKE_WORKDIR", "/tmp/sub62_smoke"))
    workdir.mkdir(parents=True, exist_ok=True)
    reg = workdir / "registry.yaml"
    reg.write_text(GOOD_V1, encoding="utf-8")
    os.environ["MODEL_REGISTRY_PATH"] = str(reg)
    mr._reset_for_tests()

    print(f"[smoke] using registry path: {reg}")

    # ── (d) hot-swap smoke ────────────────────────────────────────────────
    pick_v1 = mr._pick_model_for_role("builder", attempt_n=0)
    print(f"[d.1] initial pick (builder, attempt=0): {pick_v1!r}")
    assert pick_v1 == "qwen3-coder:480b-cloud", f"unexpected v1 pick: {pick_v1}"

    # Edit: swap primary. Bump mtime so loader sees the change.
    reg.write_text(GOOD_V2_SWAPPED, encoding="utf-8")
    new_mtime = time.time() + 10
    os.utime(reg, (new_mtime, new_mtime))

    pick_v2 = mr._pick_model_for_role("builder", attempt_n=0)
    print(f"[d.2] post-edit pick (builder, attempt=0): {pick_v2!r}")
    assert pick_v2 == "kimi-k2-thinking:cloud", f"hot-swap failed: got {pick_v2}"
    print("[d] PASS: hot-swap took effect WITHOUT daemon restart.")

    # ── (e) schema-rejection smoke ────────────────────────────────────────
    reg.write_text(CORRUPT_YAML, encoding="utf-8")
    new_mtime += 10
    os.utime(reg, (new_mtime, new_mtime))

    pick_after_corrupt = mr._pick_model_for_role("builder", attempt_n=0)
    print(
        f"[e.1] post-corruption pick (builder, attempt=0): "
        f"{pick_after_corrupt!r}"
    )
    # Cached version (kimi from v2) survives.
    assert pick_after_corrupt == "kimi-k2-thinking:cloud", (
        f"schema-reject didn't fall back to cache: got {pick_after_corrupt}"
    )
    print("[e] PASS: corrupt YAML did NOT crash; cached registry preserved.")

    print("\n[smoke] sub #62 behavioural validation: ALL GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
