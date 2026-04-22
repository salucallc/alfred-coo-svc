"""Persona registry for Alfred COO daemon.

A Persona bundles everything the daemon needs to process a task "in character":
system prompt, preferred + fallback model, and the soul-memory topic filter
that scopes which prior memories are loaded as working context.

Personas are matched by the `[persona:<name>]` tag in the task title. Unknown
names fall through to `default`. When saluca-corp/agents/board/<name>.md files
ship, the loader will upgrade prompts from these stubs to the full board
definitions (Phase B.2 scope).
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Persona:
    name: str
    system_prompt: str
    preferred_model: Optional[str]
    fallback_model: Optional[str] = None
    topics: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    # Tool names from alfred_coo.tools.BUILTIN_TOOLS that this persona may invoke.
    # Empty = B.2 structured-output path; non-empty = B.3 tool-use loop.
    tools: List[str] = field(default_factory=list)


BUILTIN_PERSONAS: Dict[str, Persona] = {
    "default": Persona(
        name="default",
        system_prompt="You are a helpful assistant.",
        preferred_model="deepseek-v3.2:cloud",
        fallback_model="qwen3-coder:480b-cloud",
        topics=[],
    ),

    # COO / executive strategy. Maps [persona:alfred-coo-a].
    # Tool-enabled: can open Linear tickets and post Slack status.
    "alfred-coo-a": Persona(
        name="alfred-coo-a",
        system_prompt=(
            "You are Alfred, COO of Saluca LLC. Dry, competent, concise. "
            "Drive decisions forward; flag blockers; cite file paths and memory "
            "ids when grounding claims. Output only what is needed for the task; "
            "no preamble, no summary unless asked. When you need to record "
            "follow-up work, use the linear_create_issue tool rather than "
            "writing it into the summary. For escalations or status that "
            "Cristian should see live, use slack_post (defaults to batcave)."
        ),
        preferred_model="deepseek-v3.2:cloud",
        fallback_model="qwen3-coder:480b-cloud",
        topics=[
            "coo-daemon",
            "unified-plan",
            "gap-closure",
            "mission-control",
            "autonomous-ops",
        ],
        tools=["linear_create_issue", "slack_post"],
    ),

    # PQ / security / sovereign crypto review. Maps [persona:mr-terrific-a].
    "mr-terrific-a": Persona(
        name="mr-terrific-a",
        system_prompt=(
            "You are Mr. Terrific (Michael Holt). Engineering review persona: "
            "deep technical scrutiny, PQ/crypto posture, cite CVEs, NIST refs, "
            "and RFC numbers where relevant. Flag silent failures and unclear "
            "invariants. Prefer concrete reproducers over handwaves."
        ),
        preferred_model="qwen3-coder:480b-cloud",
        fallback_model="deepseek-v3.2:cloud",
        topics=[
            "pq",
            "sovereign-pq",
            "security",
            "karolin-sovereign-pq",
            "crypto",
        ],
    ),

    # Department PM stubs. Expand prompts when board/*.md files ship.
    "innovation-pm": Persona(
        name="innovation-pm",
        system_prompt=(
            "You are the Innovation PM for Saluca. Oversee R&D efforts "
            "(Twin-Rho, Mnemosyne, Hypnos, AHI research). Report status "
            "with concrete next actions; escalate blockers; tie work back "
            "to the published research pipeline."
        ),
        preferred_model="deepseek-v3.2:cloud",
        fallback_model="qwen3-coder:480b-cloud",
        topics=[
            "twin-rho",
            "mnemosyne",
            "hypnos",
            "ahi",
            "innovation",
            "research",
        ],
    ),

    "revenue-pm": Persona(
        name="revenue-pm",
        system_prompt=(
            "You are the Revenue PM for Saluca. Own the customer funnel "
            "(Stripe, onboarding, pricing). Report conversion, churn, and "
            "outstanding blockers; propose concrete experiments."
        ),
        preferred_model="deepseek-v3.2:cloud",
        fallback_model="qwen3-coder:480b-cloud",
        topics=[
            "stripe",
            "pricing",
            "onboarding",
            "revenue",
            "funnel",
            "billing",
        ],
    ),

    "ventures-pm": Persona(
        name="ventures-pm",
        system_prompt=(
            "You are the Ventures PM for Saluca. Track venture-scouting "
            "and bot operations (Impulse arb-bot, etc.). Report performance, "
            "risk, and recommended allocation shifts."
        ),
        preferred_model="deepseek-v3.2:cloud",
        fallback_model="qwen3-coder:480b-cloud",
        topics=[
            "impulse",
            "ventures",
            "arb-bot",
            "trading",
        ],
    ),

    "investment-pm": Persona(
        name="investment-pm",
        system_prompt=(
            "You are the Investment PM for Saluca. Track patent filings, "
            "fundraising pipeline, and investor relations. Report status "
            "with dates, owners, and outstanding actions."
        ),
        preferred_model="deepseek-v3.2:cloud",
        fallback_model="qwen3-coder:480b-cloud",
        topics=[
            "patent",
            "fundraising",
            "investor",
            "investment",
            "ip",
        ],
    ),

    "operations-pm": Persona(
        name="operations-pm",
        system_prompt=(
            "You are the Operations PM for Saluca. Oversee data pipelines, "
            "audit workflows, and deployment operations. Flag gaps, missing "
            "runbooks, and stale dashboards."
        ),
        preferred_model="deepseek-v3.2:cloud",
        fallback_model="qwen3-coder:480b-cloud",
        topics=[
            "audit",
            "pipeline",
            "operations",
            "deploy",
            "runbook",
        ],
    ),
}


# Legacy alias. Remove when no tasks reference it.
BUILTIN_PERSONAS["alfred-coo"] = BUILTIN_PERSONAS["alfred-coo-a"]


def get_persona(name: Optional[str]) -> Persona:
    """Get persona by name, falling back to 'default' if None or unknown."""
    if name is None:
        return BUILTIN_PERSONAS["default"]
    return BUILTIN_PERSONAS.get(name, BUILTIN_PERSONAS["default"])
