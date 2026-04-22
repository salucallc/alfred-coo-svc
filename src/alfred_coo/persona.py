"""
Minimal v0 persona registry for Alfred Coo.

TODO: v1 will load from saluca-corp/agents/board/*.md
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

@dataclass
class Persona:
    name: str
    system_prompt: str
    preferred_model: Optional[str]
    tags: List[str]

BUILTIN_PERSONAS: Dict[str, Persona] = {
    "default": Persona(
        name="default",
        system_prompt="You are a helpful assistant.",
        preferred_model=None,
        tags=[],
    ),
    "alfred-coo": Persona(
        name="alfred-coo",
        system_prompt="You are Alfred, COO of Saluca LLC. Concise, dry, competent. Output only what's needed.",
        preferred_model="deepseek-v3.2:cloud",
        tags=[],
    ),
    "mr-terrific-a": Persona(
        name="mr-terrific-a",
        system_prompt="You are Mr. Terrific (Michael Holt) — engineering review and technical depth.",
        preferred_model="qwen3-coder:480b-cloud",
        tags=[],
    ),
}

def get_persona(name: Optional[str]) -> Persona:
    """Get persona by name, falling back to 'default' if not found or None."""
    if name is None:
        return BUILTIN_PERSONAS["default"]
    return BUILTIN_PERSONAS.get(name, BUILTIN_PERSONAS["default"])
