"""Budget tracker for autonomous_build (AB-05).

Accumulates per-child token spend into a cumulative USD total, fires a
warn threshold at 80% of the cap, and flips a hard-stop flag once the
cap is hit. The orchestrator reads the hard-stop flag to enter drain
mode (stop dispatching new children, let in-flight drain naturally).

Pricing table is a conservative overestimate — real rates vary with
provider + caching + discount tiers, but the budget tracker's job is to
keep us under a safe ceiling, not to match the billing system to the
cent.

Plan F section 3: $30 ceiling, 80% warn, hard-stop + drain on hit.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional


logger = logging.getLogger("alfred_coo.autonomous_build.budget")


# Per-million-token prices in USD. Input/output columns reflect typical
# Ollama-Max cloud rates (deepseek/qwen-coder cloud) + a known-free local
# entry for the 5090 model + an HF Inference Providers average.
#
# These are overestimates on purpose; if Anthropic/Ollama post cheaper
# rates mid-run we'll undercount spend, which is safer than overcounting
# and over-draining.
PRICE_PER_MTOK: Dict[str, Dict[str, float]] = {
    # Ollama Max cloud (hosted on datacenter GPUs).
    "deepseek-v3.2:cloud": {"input": 0.27, "output": 1.10},
    "qwen3-coder:480b-cloud": {"input": 0.30, "output": 1.20},
    "qwen3-coder-next:cloud": {"input": 0.30, "output": 1.20},
    "qwen3.5:397b-cloud": {"input": 0.25, "output": 1.00},
    "qwen3-next:80b-cloud": {"input": 0.15, "output": 0.60},
    "qwen3-vl:235b-cloud": {"input": 0.20, "output": 0.80},
    "kimi-k2.5:cloud": {"input": 0.30, "output": 1.20},
    "kimi-k2:1t-cloud": {"input": 0.80, "output": 2.50},
    "kimi-k2-thinking:cloud": {"input": 0.40, "output": 1.60},
    "gpt-oss:120b-cloud": {"input": 0.20, "output": 0.80},
    "gpt-oss:20b-cloud": {"input": 0.05, "output": 0.20},
    "mistral-large-3:675b-cloud": {"input": 0.80, "output": 2.50},
    "cogito-2.1:671b-cloud": {"input": 0.80, "output": 2.50},
    "devstral-2:123b-cloud": {"input": 0.25, "output": 1.00},
    "devstral-small-2:24b-cloud": {"input": 0.06, "output": 0.24},
    "nemotron-3-super:cloud": {"input": 0.20, "output": 0.80},
    "gemini-3-flash-preview:cloud": {"input": 0.10, "output": 0.40},
    # Local on 5090; no marginal cost.
    "qwen3-coder:30b-a3b-q4_K_M": {"input": 0.0, "output": 0.0},
    "gemma2:9b": {"input": 0.0, "output": 0.0},
    "llama3.1:8b": {"input": 0.0, "output": 0.0},
    "gemma4:26b": {"input": 0.0, "output": 0.0},
    "nomic-embed-text": {"input": 0.0, "output": 0.0},
    # HF Inference Providers, fastest tier average.
    "hf:openai/gpt-oss-120b:fastest": {"input": 0.20, "output": 0.80},
    "hf:openai/gpt-oss-20b:fastest": {"input": 0.05, "output": 0.20},
}

# Fallback for unknown models; conservative overestimate so we never
# under-count an unfamiliar model and blow the budget silently.
FALLBACK_PRICE = {"input": 5.0, "output": 15.0}


def estimate_cost(tokens_in: int, tokens_out: int, model: str) -> float:
    """Return the USD cost of a single completion.

    - Unknown models fall back to FALLBACK_PRICE ($5 in / $15 out per Mtok),
      erring on the side of overestimating.
    - Negative / non-numeric counts are treated as zero (and logged).
    """
    try:
        ti = int(tokens_in or 0)
        to = int(tokens_out or 0)
    except (TypeError, ValueError):
        logger.warning(
            "estimate_cost got non-numeric tokens (in=%r out=%r); treating as 0",
            tokens_in, tokens_out,
        )
        ti, to = 0, 0
    if ti < 0:
        ti = 0
    if to < 0:
        to = 0

    price = PRICE_PER_MTOK.get(model) or FALLBACK_PRICE
    return (ti / 1_000_000.0) * price["input"] + (to / 1_000_000.0) * price["output"]


class BudgetTracker:
    """Accumulates per-child spend + fires warn/hard-stop at thresholds.

    Thread-safety: not required; the orchestrator is a single asyncio task.
    """

    def __init__(
        self,
        max_usd: float = 30.0,
        warn_threshold_pct: float = 0.8,
    ) -> None:
        if max_usd <= 0:
            raise ValueError(f"max_usd must be > 0 (got {max_usd!r})")
        if not (0.0 < warn_threshold_pct < 1.0):
            raise ValueError(
                f"warn_threshold_pct must be in (0, 1) (got {warn_threshold_pct!r})"
            )
        self.max_usd: float = float(max_usd)
        self.warn_threshold_pct: float = float(warn_threshold_pct)
        self.cumulative_spend: float = 0.0
        self._warn_fired: bool = False
        self._hard_stop_fired: bool = False

    # ingestion -------------------------------------------------------

    def record(self, task_result: Dict[str, Any]) -> float:
        """Record a single completed child task's token spend.

        Reads `result.tokens.in`, `result.tokens.out`, `result.model` off
        a mesh task record (or just the `result` dict, either works).
        Missing/malformed fields are non-fatal; logs a warning and
        returns 0.0 so one broken child can't crash the tracker.
        """
        if not isinstance(task_result, dict):
            logger.warning(
                "BudgetTracker.record got non-dict %r; skipping",
                type(task_result).__name__,
            )
            return 0.0

        # Accept either a raw `result` dict or the whole mesh task rec.
        result = task_result.get("result") if "result" in task_result else task_result
        if not isinstance(result, dict):
            logger.warning("BudgetTracker.record: result is not a dict; skipping")
            return 0.0

        tokens = result.get("tokens") or {}
        if not isinstance(tokens, dict):
            logger.warning(
                "BudgetTracker.record: tokens is %r, not dict; skipping",
                type(tokens).__name__,
            )
            return 0.0

        ti = tokens.get("in")
        to = tokens.get("out")
        model = result.get("model")
        if model is None:
            logger.warning("BudgetTracker.record: missing model; skipping")
            return 0.0
        if ti is None and to is None:
            logger.warning(
                "BudgetTracker.record: tokens.in/out both missing for model=%s",
                model,
            )
            return 0.0

        cost = estimate_cost(ti or 0, to or 0, str(model))
        self.cumulative_spend += cost
        return cost

    # thresholds ------------------------------------------------------

    def check_warn(self) -> bool:
        """Return True the first time we cross the warn threshold.

        Subsequent calls return False until the tracker is reset so the
        orchestrator only posts the warn Slack message once.
        """
        if self._warn_fired:
            return False
        if self.cumulative_spend >= self.max_usd * self.warn_threshold_pct:
            self._warn_fired = True
            return True
        return False

    def check_hard_stop(self) -> bool:
        """Return True the first time cumulative spend crosses the cap.

        Subsequent calls return False (one-shot semantics) so the drain
        Slack message only goes out once, but `_hard_stop_fired` stays
        True for the rest of the run so the orchestrator's drain flag
        remains set.
        """
        if self._hard_stop_fired:
            return False
        if self.cumulative_spend >= self.max_usd:
            self._hard_stop_fired = True
            return True
        return False

    @property
    def in_drain_mode(self) -> bool:
        return self._hard_stop_fired

    @property
    def warn_fired(self) -> bool:
        return self._warn_fired

    @property
    def hard_stop_fired(self) -> bool:
        return self._hard_stop_fired

    # status view -----------------------------------------------------

    def status(self) -> Dict[str, Any]:
        pct = self.cumulative_spend / self.max_usd if self.max_usd else 0.0
        return {
            "cumulative_spend_usd": round(self.cumulative_spend, 4),
            "max_usd": self.max_usd,
            "pct_spent": round(pct, 4),
            "in_drain_mode": self.in_drain_mode,
            "warn_fired": self.warn_fired,
            "hard_stop_fired": self.hard_stop_fired,
        }

    # helpers ---------------------------------------------------------

    def set_spend(self, usd: float) -> None:
        """Test helper; jump the tracker to a specific spend so we can
        exercise threshold transitions without generating token counts."""
        self.cumulative_spend = float(usd)

    def reset(self) -> None:
        """Reset the tracker (used by restart-with-clean-budget flows)."""
        self.cumulative_spend = 0.0
        self._warn_fired = False
        self._hard_stop_fired = False


def make_tracker(payload_budget: Optional[Dict[str, Any]]) -> BudgetTracker:
    """Build a tracker from the kickoff payload's `budget` block.

    Unknown fields are ignored; missing ones use defaults.
    """
    b = payload_budget or {}
    max_usd = b.get("max_usd", 30.0)
    warn_pct = b.get("warn_threshold_pct", 0.8)
    try:
        max_usd = float(max_usd)
    except (TypeError, ValueError):
        max_usd = 30.0
    try:
        warn_pct = float(warn_pct)
    except (TypeError, ValueError):
        warn_pct = 0.8
    return BudgetTracker(max_usd=max_usd, warn_threshold_pct=warn_pct)
