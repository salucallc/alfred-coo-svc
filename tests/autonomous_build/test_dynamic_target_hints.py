"""Dynamic target-hint resolution regression tests
(refactor/dynamic-hints-from-ticket-body, 2026-04-29).

Pins the contract for ``graph._parse_target_from_ticket_body`` and
``orchestrator._resolve_target_hint`` so the refactor cannot silently
revert to a hardcoded-only state.

The refactor moves the canonical source of truth for a ticket's target
(repo / paths / new_paths / branch_hint / notes / base_branch) from the
hardcoded ``orchestrator._TARGET_HINTS`` registry into the Linear
ticket's body's ``## Target`` markdown section. The static registry
remains as a tier-2 fallback so unmodified tickets keep working.

Pinned behaviours (one section per concern):

  1. ``_parse_target_from_ticket_body`` happy path:
       * full schema -> kwargs-compatible dict for TargetHint(**parsed)
       * paths-only / new_paths-only also legal
       * inline ``# verified ...`` comments stripped from list items

  2. ``_parse_target_from_ticket_body`` rejection:
       * missing section -> None
       * missing both owner and repo -> None
       * empty paths AND empty new_paths -> None (TargetHint invariant)
       * ``(unresolved -- see plan doc)`` placeholder -> None

  3. ``_resolve_target_hint`` order:
       * body present + valid -> uses body, source="body"
       * body absent, registry hit -> uses registry, source="registry"
       * neither -> (None, "none")
       * body present but invalid -> falls back to registry
       * body parse exception -> falls back to registry (defensive)

  4. ``_render_target_block`` integration:
       * a body-supplied hint flows through the legacy renderer the
         same way a registry hint did before the refactor (byte-for-byte
         identical block shape).
"""

from __future__ import annotations

from alfred_coo.autonomous_build.graph import (
    Ticket,
    TicketStatus,
    _parse_target_from_ticket_body,
)
from alfred_coo.autonomous_build.orchestrator import (
    TargetHint,
    _render_target_block,
    _resolve_target_hint,
    _TARGET_HINTS,
)


def _ticket(
    *,
    body: str = "",
    code: str = "TEST-01",
    identifier: str = "SAL-9999",
) -> Ticket:
    """Minimal Ticket factory for the resolver tests."""
    return Ticket(
        id="uuid-" + identifier,
        identifier=identifier,
        code=code,
        title=f"{identifier} {code}: test ticket",
        wave=1,
        epic="ops",
        size="M",
        estimate=3,
        is_critical_path=False,
        labels=[],
        status=TicketStatus.PENDING,
        linear_state="Backlog",
        body=body,
    )


# ── 1. Parser happy paths ────────────────────────────────────────────


def test_parse_target_full_schema_returns_kwargs_dict() -> None:
    """Parser must emit a dict that ``TargetHint(**parsed)`` accepts
    without TypeError, with paths/new_paths as tuples (matches dataclass
    field types) and base_branch/branch_hint/notes as strings."""
    body = (
        "Some preamble.\n"
        "\n"
        "## Acceptance\n"
        "- something\n"
        "\n"
        "## Target\n"
        "\n"
        "owner: salucallc\n"
        "repo: alfred-coo-svc\n"
        "paths:\n"
        "  - src/alfred_coo/cockpit_router.py\n"
        "  - tests/test_cockpit_router.py\n"
        "new_paths:\n"
        "  - migrations/0042_consent_grants.sql\n"
        "base_branch: main\n"
        "branch_hint: feature/sal-9999-short-slug\n"
        "notes: free-form notes for the builder\n"
        "\n"
        "## Other\n"
        "ignored\n"
    )
    parsed = _parse_target_from_ticket_body(body)
    assert parsed is not None
    # TargetHint accepts the dict verbatim.
    hint = TargetHint(**parsed)
    assert hint.owner == "salucallc"
    assert hint.repo == "alfred-coo-svc"
    assert hint.paths == (
        "src/alfred_coo/cockpit_router.py",
        "tests/test_cockpit_router.py",
    )
    assert hint.new_paths == ("migrations/0042_consent_grants.sql",)
    assert hint.base_branch == "main"
    assert hint.branch_hint == "feature/sal-9999-short-slug"
    assert hint.notes == "free-form notes for the builder"


def test_parse_target_paths_only_no_new_paths() -> None:
    """A ticket that only edits existing files should parse without a
    ``new_paths`` section."""
    body = (
        "## Target\n"
        "owner: salucallc\n"
        "repo: alfred-coo-svc\n"
        "paths:\n"
        "  - src/alfred_coo/main.py\n"
        "base_branch: main\n"
    )
    parsed = _parse_target_from_ticket_body(body)
    assert parsed is not None
    hint = TargetHint(**parsed)
    assert hint.paths == ("src/alfred_coo/main.py",)
    assert hint.new_paths == ()


def test_parse_target_new_paths_only_pure_creation() -> None:
    """OPS-02-style pure-creation tickets must parse without ``paths``."""
    body = (
        "## Target\n"
        "owner: salucallc\n"
        "repo: alfred-coo-svc\n"
        "new_paths:\n"
        "  - deploy/appliance/IMAGE_PINS.md\n"
    )
    parsed = _parse_target_from_ticket_body(body)
    assert parsed is not None
    hint = TargetHint(**parsed)
    assert hint.paths == ()
    assert hint.new_paths == ("deploy/appliance/IMAGE_PINS.md",)
    # Default base_branch.
    assert hint.base_branch == "main"


def test_parse_target_strips_inline_comments_from_list_items() -> None:
    """The verified-render mode emits ``  - path  # verified exists @ main``
    lines; if a builder copies one of those back into a ticket body, the
    parser must strip the trailing comment so the path stays usable."""
    body = (
        "## Target\n"
        "owner: salucallc\n"
        "repo: alfred-coo-svc\n"
        "paths:\n"
        "  - deploy/appliance/docker-compose.yml  # verified exists @ main\n"
        "  - deploy/appliance/Caddyfile\n"
    )
    parsed = _parse_target_from_ticket_body(body)
    assert parsed is not None
    assert parsed["paths"] == (
        "deploy/appliance/docker-compose.yml",
        "deploy/appliance/Caddyfile",
    )


def test_parse_target_handles_linear_markdown_round_trip() -> None:
    """Linear's web-UI markdown renderer normalises tight ``- foo``
    lists into loose ``* foo`` lists with a paragraph break after the
    parent ``paths:`` key. The parser must round-trip cleanly: a body
    we POST as ``  - foo`` may come back as ``\n\n* foo\n``, and both
    representations MUST produce the same parsed dict.

    Pinned because the 2026-04-29 backfill round-trip exposed it: 76
    tickets posted with ``-`` markers came back with ``*`` markers and
    a blank line after ``paths:``, breaking the parser before this
    fix. See backfill_report.json from the dry-run cycle.
    """
    body_round_tripped = (
        "## Target\n"
        "\n"
        "owner: salucallc\n"
        "repo: cockpit\n"
        "paths:\n"
        "\n"
        "* apps/cockpit/copy/registers.json\n"
        "* apps/cockpit/lib/useCopy.ts\n"
        "  base_branch: main\n"
        "  notes: (auto-derived from ticket body; review on first dispatch)\n"
    )
    parsed = _parse_target_from_ticket_body(body_round_tripped)
    assert parsed is not None
    assert parsed["owner"] == "salucallc"
    assert parsed["repo"] == "cockpit"
    assert parsed["paths"] == (
        "apps/cockpit/copy/registers.json",
        "apps/cockpit/lib/useCopy.ts",
    )
    assert parsed["base_branch"] == "main"
    assert parsed["notes"].startswith("(auto-derived")


# ── 2. Parser rejection paths ────────────────────────────────────────


def test_parse_target_missing_section_returns_none() -> None:
    """A ticket body with no ``## Target`` heading must parse to None."""
    body = (
        "## Acceptance\n"
        "- The widget MUST flange.\n"
        "\n"
        "## Plan\n"
        "Do the thing.\n"
    )
    assert _parse_target_from_ticket_body(body) is None


def test_parse_target_missing_owner_or_repo_returns_none() -> None:
    """``owner`` and ``repo`` are non-negotiable; without them the resolver
    must fall through to the registry, not raise on TargetHint(...)."""
    body_no_owner = (
        "## Target\n"
        "repo: alfred-coo-svc\n"
        "paths:\n"
        "  - src/x.py\n"
    )
    body_no_repo = (
        "## Target\n"
        "owner: salucallc\n"
        "paths:\n"
        "  - src/x.py\n"
    )
    assert _parse_target_from_ticket_body(body_no_owner) is None
    assert _parse_target_from_ticket_body(body_no_repo) is None


def test_parse_target_empty_paths_and_new_paths_returns_none() -> None:
    """A header + owner + repo with no path lists is useless to the
    builder; parser must return None so resolver falls through."""
    body = (
        "## Target\n"
        "owner: salucallc\n"
        "repo: alfred-coo-svc\n"
        "base_branch: main\n"
    )
    assert _parse_target_from_ticket_body(body) is None


def test_parse_target_unresolved_placeholder_returns_none() -> None:
    """The planner sub may emit ``(unresolved -- see plan doc)`` for
    tickets it cannot ground; the parser must treat that as 'no body
    hint' so the resolver escalates the same way it always has."""
    body_unresolved = (
        "## Target\n"
        "(unresolved -- see plan doc)\n"
    )
    assert _parse_target_from_ticket_body(body_unresolved) is None


def test_parse_target_empty_body_returns_none() -> None:
    """Defensive: empty / None body must short-circuit cleanly."""
    assert _parse_target_from_ticket_body("") is None
    assert _parse_target_from_ticket_body(None) is None


# ── 3. Resolver order (body > registry > none) ───────────────────────


def test_resolver_prefers_body_when_both_sources_present() -> None:
    """Priority is body > registry. Pick a code that exists in
    ``_TARGET_HINTS`` so we can prove the body wins."""
    # Find any registry entry to use as the "registry" leg.
    registry_code = next(iter(_TARGET_HINTS))
    body = (
        "## Target\n"
        "owner: cristianxruvalcaba-coder\n"  # different from registry
        "repo: BODY-WINS-REPO\n"
        "paths:\n"
        "  - body/wins/path.py\n"
        "base_branch: main\n"
        "branch_hint: feature/body-wins\n"
    )
    ticket = _ticket(body=body, code=registry_code)
    hint, source = _resolve_target_hint(ticket)
    assert source == "body"
    assert hint is not None
    assert hint.repo == "BODY-WINS-REPO"
    assert hint.paths == ("body/wins/path.py",)


def test_resolver_falls_back_to_registry_when_body_absent() -> None:
    """Registry tier still works for the legacy / unmigrated tickets."""
    registry_code = next(iter(_TARGET_HINTS))
    ticket = _ticket(body="No target section here at all.", code=registry_code)
    hint, source = _resolve_target_hint(ticket)
    assert source == "registry"
    assert hint is _TARGET_HINTS[registry_code]


def test_resolver_returns_none_when_neither_present() -> None:
    """An unparseable / unmapped ticket must produce (None, "none") so
    the dispatch path emits the (unresolved) escalation block."""
    ticket = _ticket(body="empty", code="UNKNOWN-CODE-XYZ")
    hint, source = _resolve_target_hint(ticket)
    assert hint is None
    assert source == "none"


def test_resolver_falls_back_when_body_invalid() -> None:
    """A body section that omits owner+repo (parser returns None) must
    fall through to the registry tier rather than crashing or holding
    the resolver in a bad state."""
    registry_code = next(iter(_TARGET_HINTS))
    body = (
        "## Target\n"
        "(unresolved -- see plan doc)\n"
    )
    ticket = _ticket(body=body, code=registry_code)
    hint, source = _resolve_target_hint(ticket)
    assert source == "registry"
    assert hint is _TARGET_HINTS[registry_code]


def test_resolver_falls_back_when_body_violates_targethint_invariant(
    monkeypatch,
) -> None:
    """If the parser returns a dict but TargetHint validation still
    fails (forward-compat: parser starts emitting a key the dataclass
    doesn't accept), the resolver must catch the TypeError and fall
    through. Simulate by monkeypatching the parser to return a bogus
    extra key."""
    import alfred_coo.autonomous_build.orchestrator as orch_mod

    def fake_parser(body):
        return {
            "owner": "salucallc",
            "repo": "alfred-coo-svc",
            "paths": ("src/x.py",),
            "future_field_we_dont_have_yet": "boom",  # TypeError on TargetHint(**)
        }

    monkeypatch.setattr(orch_mod, "_parse_target_from_ticket_body", fake_parser)
    registry_code = next(iter(_TARGET_HINTS))
    ticket = _ticket(body="## Target\nowner: x\nrepo: y\n", code=registry_code)
    hint, source = _resolve_target_hint(ticket)
    assert source == "registry"
    assert hint is _TARGET_HINTS[registry_code]


def test_resolver_handles_parser_exception(monkeypatch) -> None:
    """A crashing parser must not break dispatch — fall through to the
    registry tier with a logged exception."""
    import alfred_coo.autonomous_build.orchestrator as orch_mod

    def boom(body):
        raise RuntimeError("parser blew up")

    monkeypatch.setattr(orch_mod, "_parse_target_from_ticket_body", boom)
    registry_code = next(iter(_TARGET_HINTS))
    ticket = _ticket(
        body="## Target\nowner: x\nrepo: y\npaths:\n  - z\n", code=registry_code,
    )
    hint, source = _resolve_target_hint(ticket)
    assert source == "registry"
    assert hint is _TARGET_HINTS[registry_code]


# ── 4. Render integration (body-supplied hint flows through) ────────


def test_render_target_block_consumes_body_hint() -> None:
    """A body-resolved hint passed through ``hint=`` must render the
    same way a registry hint renders today (no vr): ``## Target`` +
    owner/repo/paths/base_branch/branch_hint/notes."""
    body = (
        "## Target\n"
        "owner: salucallc\n"
        "repo: alfred-coo-svc\n"
        "paths:\n"
        "  - src/alfred_coo/cockpit_router.py\n"
        "new_paths:\n"
        "  - migrations/0042_consent_grants.sql\n"
        "base_branch: main\n"
        "branch_hint: feature/sal-9999-short-slug\n"
        "notes: parser-emitted body hint\n"
    )
    parsed = _parse_target_from_ticket_body(body)
    assert parsed is not None
    hint = TargetHint(**parsed)
    block = _render_target_block(code="DYN-01", vr=None, hint=hint)
    assert "## Target" in block
    assert "owner: salucallc" in block
    assert "repo:  alfred-coo-svc" in block
    assert "src/alfred_coo/cockpit_router.py" in block
    assert "base_branch: main" in block
    assert "branch_hint: feature/sal-9999-short-slug" in block
    assert "notes: parser-emitted body hint" in block


def test_render_target_block_no_hint_falls_through_to_registry() -> None:
    """Default behaviour preserved: when ``hint`` kwarg is absent, the
    renderer must still consult ``_TARGET_HINTS`` (legacy callers + the
    snapshot tests rely on this)."""
    registry_code = next(iter(_TARGET_HINTS))
    block = _render_target_block(code=registry_code, vr=None)
    assert "## Target" in block
    expected_repo = _TARGET_HINTS[registry_code].repo
    assert expected_repo in block


def test_render_target_block_unresolved_when_neither_present() -> None:
    """Snapshot regression: with no hint kwarg AND a code that is not in
    the registry, the renderer must emit the same legacy ``(unresolved
    -- consult plan doc...)`` block so existing tests (and the child's
    Step-0 grounding-gap escalation) keep firing."""
    block = _render_target_block(code="DEFINITELY-NOT-IN-REGISTRY-XYZ", vr=None)
    assert "(unresolved" in block
    assert "STOP and escalate" in block


# ── 5. Pluggable resolver registry contract ─────────────────────────


def test_resolver_registry_tier_order_pinned() -> None:
    """alfred-main directive (2026-04-30): the resolver list MUST stay
    layered so a future vision-aware tier can plug in BELOW the registry.
    Pin tier 0 = body and tier 1 = registry so a future PR can append
    without rewriting these guarantees."""
    from alfred_coo.autonomous_build.orchestrator import _TARGET_HINT_RESOLVERS

    assert len(_TARGET_HINT_RESOLVERS) >= 2
    assert _TARGET_HINT_RESOLVERS[0][0] == "body"
    assert _TARGET_HINT_RESOLVERS[1][0] == "registry"


def test_resolver_appended_tier_runs_when_earlier_tiers_miss() -> None:
    """A future resolver appended to ``_TARGET_HINT_RESOLVERS`` must be
    consulted when body + registry both return None. Simulate by
    appending a stub tier that always returns a sentinel hint, then
    pop it back out so we don't pollute the module."""
    import alfred_coo.autonomous_build.orchestrator as orch_mod

    sentinel = TargetHint(
        owner="future",
        repo="vision-aware",
        paths=("inferred/path.py",),
        base_branch="main",
        notes="injected by stub tier",
    )
    stub_calls = []

    def stub_resolver(ticket):
        stub_calls.append(ticket.identifier)
        return sentinel

    orch_mod._TARGET_HINT_RESOLVERS.append(("ai-vision-stub", stub_resolver))
    try:
        ticket = _ticket(body="no target section", code="DEFINITELY-MISSING-XYZ")
        hint, source = _resolve_target_hint(ticket)
        assert source == "ai-vision-stub"
        assert hint is sentinel
        assert stub_calls == [ticket.identifier]
    finally:
        orch_mod._TARGET_HINT_RESOLVERS.pop()


def test_resolver_appended_tier_skipped_when_body_or_registry_hits() -> None:
    """The appended tier must NOT run if an earlier tier already
    produced a hint — first-match-wins semantics."""
    import alfred_coo.autonomous_build.orchestrator as orch_mod

    stub_calls = []

    def stub_resolver(ticket):
        stub_calls.append(ticket.identifier)
        return TargetHint(
            owner="future",
            repo="vision-aware",
            paths=("inferred.py",),
        )

    orch_mod._TARGET_HINT_RESOLVERS.append(("ai-vision-stub", stub_resolver))
    try:
        # Tier 1 (body) hits → tier 3 (stub) must NOT run.
        body = (
            "## Target\n"
            "owner: salucallc\n"
            "repo: alfred-coo-svc\n"
            "paths:\n"
            "  - src/x.py\n"
        )
        ticket = _ticket(body=body, code="DOES-NOT-MATTER")
        hint, source = _resolve_target_hint(ticket)
        assert source == "body"
        assert stub_calls == []

        # Tier 2 (registry) hits → tier 3 must NOT run.
        registry_code = next(iter(_TARGET_HINTS))
        ticket2 = _ticket(body="", code=registry_code)
        hint2, source2 = _resolve_target_hint(ticket2)
        assert source2 == "registry"
        assert stub_calls == []
    finally:
        orch_mod._TARGET_HINT_RESOLVERS.pop()


# ── SAL-3740: parser resilience to format drift ────────────────────────────


def test_parse_target_accepts_parenthetical_between_key_and_colon() -> None:
    """SAL-3740: path-fix subs sometimes emit lines like

        new_paths (you will CREATE all of these — directory not yet
        populated):

    instead of the canonical ``new_paths:``. Pre-fix, the parser's
    ``key: value`` regex required immediate ``:`` after the key word
    and silently dropped 13 of 14 wave-2 tickets into NO_HINT, mass-
    crashing four Phase 2 kickoffs. Now the parser tolerates an
    optional parenthetical between the key word and the colon.
    """
    body = (
        "## Target\n"
        "\n"
        "owner: salucallc\n"
        "repo: alfred-coo-svc\n"
        "base_branch: main\n"
        "\n"
        "new_paths (you will CREATE all of these — bootstrap dir):\n"
        "  - plugins/saluca-plugin-langchain/setup.py\n"
        "  - plugins/saluca-plugin-langchain/src/__init__.py\n"
    )
    parsed = _parse_target_from_ticket_body(body)
    assert parsed is not None, (
        "parenthetical between key and colon should not break parsing"
    )
    assert parsed["owner"] == "salucallc"
    assert parsed["new_paths"] == (
        "plugins/saluca-plugin-langchain/setup.py",
        "plugins/saluca-plugin-langchain/src/__init__.py",
    )


def test_parse_target_paths_with_parenthetical_also_accepted() -> None:
    """Symmetry: same parenthetical tolerance for ``paths:``."""
    body = (
        "## Target\n"
        "owner: salucallc\n"
        "repo: alfred-coo-svc\n"
        "paths (already exist — verify before editing):\n"
        "  - src/alfred_coo/persona.py\n"
    )
    parsed = _parse_target_from_ticket_body(body)
    assert parsed is not None
    assert parsed["paths"] == ("src/alfred_coo/persona.py",)


def test_parse_target_canonical_form_still_works() -> None:
    """Regression guard: the parenthetical relaxation must not break
    the canonical ``key:`` form (no parens) that the planner sub emits."""
    body = (
        "## Target\n"
        "owner: salucallc\n"
        "repo: alfred-coo-svc\n"
        "paths:\n"
        "  - a.py\n"
        "new_paths:\n"
        "  - b.py\n"
    )
    parsed = _parse_target_from_ticket_body(body)
    assert parsed is not None
    assert parsed["paths"] == ("a.py",)
    assert parsed["new_paths"] == ("b.py",)
