"""AB-22 umbrella _TARGET_HINTS regression tests (Gap 6, 2026-04-29).

Pin the AB-22 umbrella hint contents so future edits do not regress to
the plan-only state that produced PRs #272/#275/#276/#277/#287/#288/#289
(builders correctly followed the hint and shipped plan-only PRs because
the hint listed only ``plans/v1-ga/AB-22.md`` as new_paths; Hawkman
correctly REQUEST_CHANGES because plan-only docs aren't acceptance-
passing implementations).

``_CODE_RE`` collapses every AB-22-{a..g} child title to the single
parsed code ``AB-22`` (the regex captures at most one trailing letter
and the dash-letter suffix is stripped), so the umbrella entry IS the
per-child hint surface — there is no architectural way to register a
separate hint per child without first widening the parser. This mirrors
the existing OPS-14D umbrella pattern at lines ~2605-2627.

Pins five behaviours:
  1. AB-22-d (SAL-2741) implementation files appear in ``new_paths``
     (Soulkey registry + consumers.yaml at minimum).
  2. AB-22-g (SAL-2760) artefact appears in ``new_paths`` (the native
     ``/v1/messages`` passthrough test, since the route itself extends
     AB-22-c's ``routes.py``).
  3. ``new_paths`` is not the legacy plan-only set
     ``{"plans/v1-ga/AB-22.md"}`` and contains real source files.
  4. The umbrella entry still exists and points at
     ``salucallc/alfred-coo-svc`` with a ``feature/ab-22-*`` branch hint
     (no accidental key-renaming or repo-flip regression).
  5. ``_TARGET_HINTS`` smoke-loads (no dataclass invariant violation
     from ``TargetHint.__post_init__`` after the edit).
"""

from __future__ import annotations

from alfred_coo.autonomous_build.orchestrator import _TARGET_HINTS


# ── 1. AB-22-d (SAL-2741) — Soulkey registry + bootstrap + consumers.yaml ──


def test_ab22_hint_covers_sal_2741_soulkey_files() -> None:
    """SAL-2741 ships ``tiresias_proxy/soulkeys.py`` + ``deploy/appliance/
    consumers.yaml`` per the Linear ticket spec; both must appear in the
    umbrella ``new_paths`` so the dispatched builder sees real
    implementation targets, not just the plan doc.
    """
    hint = _TARGET_HINTS["AB-22"]
    assert "src/alfred_coo/tiresias_proxy/soulkeys.py" in hint.new_paths
    assert "deploy/appliance/consumers.yaml" in hint.new_paths


# ── 2. AB-22-g (SAL-2760) — native POST /v1/messages passthrough ──


def test_ab22_hint_covers_sal_2760_messages_passthrough() -> None:
    """SAL-2760 extends AB-22-c's ``routes.py`` with the Anthropic-native
    ``/v1/messages`` route; the route file itself is owned by AB-22-c so
    the child-specific artefact pinned here is the dedicated passthrough
    test under ``tests/tiresias_proxy/``.
    """
    hint = _TARGET_HINTS["AB-22"]
    assert (
        "tests/tiresias_proxy/test_messages_passthrough.py" in hint.new_paths
    )
    # AB-22-c's routes.py is also in new_paths (where the route lands).
    assert "src/alfred_coo/tiresias_proxy/routes.py" in hint.new_paths


# ── 3. new_paths is not the legacy plan-only set ──


def test_ab22_hint_new_paths_not_plan_only() -> None:
    """Pre-fix regression guard: the umbrella's ``new_paths`` must not
    collapse back to the single-file plan-doc-only set that produced the
    plan-only PR storm (#272 et al). Demands ≥2 implementation files
    plus the plan doc.
    """
    hint = _TARGET_HINTS["AB-22"]
    plan_doc = "plans/v1-ga/AB-22.md"
    assert plan_doc in hint.new_paths, (
        "umbrella plan doc still listed so the first child to land "
        "creates it; later children collide via NEW_PATHS_COLLISION"
    )
    real_paths = [p for p in hint.new_paths if p != plan_doc]
    assert len(real_paths) >= 2, (
        f"new_paths regressed to plan-only-style; got {hint.new_paths!r}"
    )
    # At least one Python source under the new tiresias_proxy package.
    assert any(
        p.startswith("src/alfred_coo/tiresias_proxy/") and p.endswith(".py")
        for p in real_paths
    ), f"no tiresias_proxy/*.py source files in new_paths: {real_paths!r}"


# ── 4. Umbrella entry shape preserved (regression guard) ──


def test_ab22_umbrella_entry_shape_preserved() -> None:
    """No accidental owner/repo flip or branch-hint deletion, and at
    least one ``paths`` entry is present so the verifier has an anchor
    to confirm the package parent + appliance assets exist."""
    hint = _TARGET_HINTS["AB-22"]
    assert hint.owner == "salucallc"
    assert hint.repo == "alfred-coo-svc"
    assert hint.base_branch == "main"
    assert hint.branch_hint and hint.branch_hint.startswith("feature/ab-22")
    assert hint.notes, "notes must document per-child scope split"
    # ``paths`` must include the package anchor so the verifier can
    # confirm ``src/alfred_coo`` exists at dispatch time.
    assert "src/alfred_coo/__init__.py" in hint.paths


# ── 5. Smoke: orchestrator module loads with new hint ──


def test_target_hints_table_smoke_loads() -> None:
    """Importing ``_TARGET_HINTS`` exercises every ``TargetHint``
    dataclass's ``__post_init__`` (which raises if both ``paths`` and
    ``new_paths`` are empty). Smoke test: at least one hint per major
    code family is loadable and the AB-22 entry survived the edit.
    """
    assert "AB-22" in _TARGET_HINTS
    # Spot-check a handful of unrelated families to confirm the table
    # didn't get corrupted by the AB-22 edit (defensive).
    for code in ("OPS-14D", "AD-A", "F08", "TIR-01"):
        assert code in _TARGET_HINTS, f"{code!r} missing from _TARGET_HINTS"
        h = _TARGET_HINTS[code]
        assert h.owner and h.repo
        assert h.paths or h.new_paths
