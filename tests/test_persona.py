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
    assert p.preferred_model == "qwen3-coder:480b-cloud"
    assert p.fallback_model == "deepseek-v3.2:cloud"
    assert "coo-daemon" in p.topics


def test_legacy_alias_resolves_to_alfred_coo_a():
    a = get_persona("alfred-coo")
    b = get_persona("alfred-coo-a")
    assert a is b


def test_mr_terrific_a_no_longer_resolves():
    """Phase B.3.5: mr-terrific-a removed. Canonical DC_ORG_MAP assigns
    Mr. Terrific to VP Product; crypto work lives with riddler-crypto-a."""
    p = get_persona("mr-terrific-a")
    assert p.name == "default"


def test_all_personas_have_fallback_distinct_from_preferred():
    for name, p in BUILTIN_PERSONAS.items():
        if p.preferred_model is None:
            continue
        assert p.fallback_model != p.preferred_model, (
            f"persona {name} has identical preferred/fallback model"
        )


def test_all_pm_personas_have_topics():
    """Phase B.3.5: PM-role personas renamed to canonical DC characters."""
    for name in (
        "maxwell-lord-a",
        "starfire-ventures-a",
        "lucius-fox-a",
        "sawyer-ops-a",
        "red-robin-a",
    ):
        p = get_persona(name)
        assert p.name == name, f"{name} did not resolve"
        assert p.topics, f"{name} has empty topics list"


def test_alfred_coo_a_has_b3_tools():
    """B.3.1+B.3.2+B.3.4: alfred-coo-a opts into tool-use with all five tools."""
    p = get_persona("alfred-coo-a")
    assert p.tools, "alfred-coo-a should have tool-use enabled"
    for expected in ("linear_create_issue", "slack_post", "mesh_task_create", "propose_pr", "http_get"):
        assert expected in p.tools, f"alfred-coo-a missing tool: {expected}"


def test_default_persona_has_no_tools():
    """Safety: default persona must NOT auto-invoke tools."""
    p = get_persona("default")
    assert p.tools == []


def test_riddler_crypto_has_five_tools():
    """B.3.5: riddler-crypto-a replaces mr-terrific-a and carries the full
    builder toolchain for PQ/crypto work."""
    p = get_persona("riddler-crypto-a")
    assert p.name == "riddler-crypto-a"
    expected = {"linear_create_issue", "slack_post", "mesh_task_create", "propose_pr", "http_get"}
    assert set(p.tools) == expected, f"riddler-crypto-a tools mismatch: {p.tools}"
    assert len(p.tools) == 5


def test_hawkman_qa_has_pr_review():
    """B.3.5: hawkman-qa-a is an independent verifier — pr_review is required."""
    p = get_persona("hawkman-qa-a")
    assert p.name == "hawkman-qa-a"
    assert "pr_review" in p.tools


def test_batgirl_sec_has_pr_review():
    """B.3.5: batgirl-sec-a is an independent security verifier — pr_review is required."""
    p = get_persona("batgirl-sec-a")
    assert p.name == "batgirl-sec-a"
    assert "pr_review" in p.tools


def test_batman_and_steel_are_advisory():
    """B.3.5: CISO and CTO are advisory only — Slack + Linear, no code-write tools."""
    for name in ("batman-ciso-a", "steel-cto-a"):
        p = get_persona(name)
        assert p.name == name, f"{name} did not resolve"
        assert p.tools == ["slack_post", "linear_create_issue"], (
            f"{name} should be advisory-only; got tools={p.tools}"
        )


def test_hawkman_qa_has_pr_files_get():
    """B.3.6: hawkman-qa-a gets pr_files_get so private-repo PR review works."""
    p = get_persona("hawkman-qa-a")
    assert "pr_files_get" in p.tools


def test_batgirl_sec_has_pr_files_get():
    """B.3.6: batgirl-sec-a gets pr_files_get so private-repo PR review works."""
    p = get_persona("batgirl-sec-a")
    assert "pr_files_get" in p.tools


# ── AB-01: autonomous-build-a registration + handler field ──────────────────


def test_autonomous_build_a_registers_with_handler():
    """AB-01: autonomous-build-a opts into the long-running orchestrator path
    via the new Persona.handler field."""
    p = get_persona("autonomous-build-a")
    assert p.name == "autonomous-build-a"
    assert p.handler == "AutonomousBuildOrchestrator"


def test_autonomous_build_a_routing_and_topics():
    """AB-01: autonomous-build-a routes to qwen3-coder:480b-cloud with the
    local 30b-a3b fallback, and carries the Mission Control v1 GA topics."""
    p = get_persona("autonomous-build-a")
    assert p.preferred_model == "qwen3-coder:480b-cloud"
    assert p.fallback_model == "qwen3-coder:30b-a3b-q4_K_M"
    assert "autonomous_build" in p.topics
    assert "mission-control-v1-ga" in p.topics


def test_handler_field_defaults_to_none_for_existing_personas():
    """AB-01: the new Persona.handler field must default to None so no
    existing persona is accidentally promoted into the long-running path."""
    for name, p in BUILTIN_PERSONAS.items():
        if name == "autonomous-build-a":
            continue
        assert p.handler is None, (
            f"persona {name} unexpectedly has handler={p.handler!r}; "
            "only autonomous-build-a should opt into the long-running path "
            "in AB-01."
        )
