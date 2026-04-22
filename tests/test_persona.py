"""Persona registry tests."""

from alfred_coo.persona import BUILTIN_PERSONAS, get_persona


def test_default_persona_returned_on_none():
    p = get_persona(None)
    assert p.name == "default"


def test_default_persona_returned_on_unknown():
    p = get_persona("does-not-exist")
    assert p.name == "default"


def test_alfred_coo_a_resolves():
    p = get_persona("alfred-coo-a")
    assert p.name == "alfred-coo-a"
    assert p.preferred_model == "deepseek-v3.2:cloud"
    assert p.fallback_model is not None
    assert p.fallback_model != p.preferred_model
    assert "coo-daemon" in p.topics


def test_legacy_alias_resolves_to_alfred_coo_a():
    a = get_persona("alfred-coo")
    b = get_persona("alfred-coo-a")
    assert a is b


def test_mr_terrific_a_has_pq_topics():
    p = get_persona("mr-terrific-a")
    assert p.name == "mr-terrific-a"
    assert "pq" in p.topics
    assert "security" in p.topics


def test_all_personas_have_fallback_distinct_from_preferred():
    for name, p in BUILTIN_PERSONAS.items():
        if p.preferred_model is None:
            continue
        assert p.fallback_model != p.preferred_model, (
            f"persona {name} has identical preferred/fallback model"
        )


def test_all_pm_personas_have_topics():
    for name in ("innovation-pm", "revenue-pm", "ventures-pm", "investment-pm", "operations-pm"):
        p = get_persona(name)
        assert p.name == name, f"{name} did not resolve"
        assert p.topics, f"{name} has empty topics list"


def test_alfred_coo_a_has_b3_tools():
    """B.3.1+B.3.2: alfred-coo-a opts into tool-use with all four tools."""
    p = get_persona("alfred-coo-a")
    assert p.tools, "alfred-coo-a should have tool-use enabled"
    for expected in ("linear_create_issue", "slack_post", "mesh_task_create", "propose_pr"):
        assert expected in p.tools, f"alfred-coo-a missing tool: {expected}"


def test_default_persona_has_no_tools():
    """Safety: default persona must NOT auto-invoke tools."""
    p = get_persona("default")
    assert p.tools == []
