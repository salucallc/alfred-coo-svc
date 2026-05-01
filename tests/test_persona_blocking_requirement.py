"""Tests for the BLOCKING REQUIREMENT block in alfred-coo-a builder persona.

Mining-sub findings 2026-04-28: 84% of Hawkman REQUEST_CHANGES reviews
(47/56 in 7d) are 'missing APE/V citation'. This test locks in the
BLOCKING REQUIREMENT prompt block so future edits don't accidentally
remove it, and pairs with .github/workflows/pr-body-apev-lint.yml which
fail-closes any PR missing the heading at PR-creation time.
"""

from alfred_coo.persona import get_persona


def test_alfred_coo_a_has_blocking_requirement_block():
    """The builder persona must start with a BLOCKING REQUIREMENT block
    that calls out the APE/V citation before any STEP instructions."""
    p = get_persona("alfred-coo-a")
    prompt = p.system_prompt
    assert "BLOCKING REQUIREMENT" in prompt, (
        "alfred-coo-a system_prompt is missing the BLOCKING REQUIREMENT "
        "block. This block is the primary enforcement mechanism for the "
        "APE/V citation; do not remove it without coordinated CI + Hawkman "
        "prompt updates."
    )


def test_alfred_coo_a_has_canonical_apev_heading():
    """The exact heading text Hawkman gate-1 expects must appear in the
    persona prompt so builders know what to emit verbatim."""
    p = get_persona("alfred-coo-a")
    prompt = p.system_prompt
    assert "## APE/V Acceptance (machine-checkable)" in prompt, (
        "alfred-coo-a system_prompt is missing the canonical heading "
        "'## APE/V Acceptance (machine-checkable)'. Hawkman gate-1 "
        "performs a verbatim substring match against the Linear ticket "
        "body using this exact phrasing; the persona prompt must show "
        "the heading byte-exactly so builders copy it correctly."
    )


def test_blocking_requirement_appears_before_step_0():
    """The BLOCKING REQUIREMENT block must appear before STEP 0 so it is
    the first instruction the builder reads. If a future edit moves it
    after the steps, builders that quit-reading-early may skip it."""
    p = get_persona("alfred-coo-a")
    prompt = p.system_prompt
    blocking_idx = prompt.find("BLOCKING REQUIREMENT")
    step0_idx = prompt.find("STEP 0")
    assert blocking_idx >= 0, "BLOCKING REQUIREMENT block missing"
    assert step0_idx >= 0, "STEP 0 missing"
    assert blocking_idx < step0_idx, (
        "BLOCKING REQUIREMENT block must precede STEP 0; current ordering "
        "puts STEP 0 first which weakens the blocking signal."
    )


def test_blocking_requirement_names_both_pr_tools():
    """The block must reference propose_pr and update_pr by name so the
    builder knows it applies to both PR-opening and PR-updating paths."""
    p = get_persona("alfred-coo-a")
    prompt = p.system_prompt
    # Slice to just the BLOCKING REQUIREMENT block (ends at FOLLOW THIS PROTOCOL).
    start = prompt.find("BLOCKING REQUIREMENT")
    end = prompt.find("FOLLOW THIS PROTOCOL")
    assert start >= 0 and end > start
    block = prompt[start:end]
    assert "propose_pr" in block
    assert "update_pr" in block


def test_registry_entry_consistent_with_legacy_alias():
    """alfred-coo (legacy alias) must resolve to the same persona object,
    so the BLOCKING REQUIREMENT applies to both names."""
    a = get_persona("alfred-coo")
    b = get_persona("alfred-coo-a")
    assert a is b
    assert "BLOCKING REQUIREMENT" in a.system_prompt


def test_alfred_coo_a_has_http_get_hard_cap():
    """SAL-3802: builder must be told 2-consecutive http_get is the cap.
    The soft "after your 4th http_get" guidance was insufficient — 6/6
    fleet dispatches 2026-05-01 bailed on http_get x4 with zero
    propose_pr calls. The HARD CAP block makes the rule unambiguous."""
    p = get_persona("alfred-coo-a")
    prompt = p.system_prompt
    assert "HARD CAP on http_get" in prompt, (
        "alfred-coo-a system_prompt is missing the HARD CAP on http_get "
        "block. This block is the SAL-3802 fix and must be present "
        "verbatim so the cap is unambiguous to all builder models."
    )
    assert "at most 2 CONSECUTIVE http_get calls" in prompt, (
        "HARD CAP block must state the 2-consecutive-call cap verbatim."
    )


def test_alfred_coo_a_has_linear_create_issue_not_an_escape_rule():
    """SAL-3795: builder must be told linear_create_issue is reserved for
    real grounding gaps, not as an escape from hard tasks. 2026-05-01
    fleet ran into legitimate AND illegitimate uses of the tool; this
    rule helps the model self-discriminate before tool-calling."""
    p = get_persona("alfred-coo-a")
    prompt = p.system_prompt
    assert "linear_create_issue is NOT an escape" in prompt, (
        "alfred-coo-a system_prompt is missing the linear_create_issue "
        "NOT-an-escape rule. This is the SAL-3795 persona-prompt fix."
    )
    # The rule must articulate the self-check question that gates the call.
    assert "could I write a reasonable propose_pr from what I have" in prompt

