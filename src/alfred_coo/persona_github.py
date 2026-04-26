"""Per-persona GitHub identity routing (SAL-2905).

A single ``GITHUB_TOKEN`` env var, owned by ``cristianxruvalcaba-coder``,
was used for every GitHub interaction in this daemon: builder personas
opening PRs (``propose_pr`` / ``update_pr``), QA personas posting
reviews (``pr_review``), the orchestrator merging (``github_merge_pr``),
and read-only probes (``http_get``, hint-verification). With one
identity for all of them, every PR's author == its reviewer, GitHub's
``/reviews`` endpoint returns 422 ("Can not approve your own pull
request"), ``pr_review`` falls back to a non-blocking issue-comment
prefixed ``(fallback - self-authored PR)``, and the orchestrator
refuses to merge. Empirical: v7s 4/4 PRs blocked; wave-1 ratio 0.07.

This module routes each persona to an *identity class* (builder / QA /
orchestrator), and resolves a per-class token from the environment.
The dispatch loop (``main.py``) sets ``_current_persona`` for the
duration of each tool-use call, and tool handlers
(``propose_pr``, ``pr_review``, etc.) consume it via
``token_for_persona`` instead of reading ``GITHUB_TOKEN`` directly.

Backwards compatibility: when only ``GITHUB_TOKEN`` is set,
``token_for_persona`` returns the legacy token for every class, so
single-token deployments behave exactly as today (including the
self-authored fallback). The fix lands when an operator adds
``GITHUB_TOKEN_QA`` to ``/etc/alfred-coo/.env``.

Adding a new identity is a one-line change to ``PERSONA_IDENTITY_MAP``.
"""

from __future__ import annotations

import contextvars
import logging
import os
from typing import Optional


logger = logging.getLogger("alfred_coo.persona_github")


# ── Identity classes ────────────────────────────────────────────────────────

class GitHubIdentityClass:
    """String tags for the env-var lookup; not an enum so the values are
    tunnel-stable as plain strings in logs / tests / config files."""

    BUILDER = "builder"
    QA = "qa"
    ORCHESTRATOR = "orchestrator"
    UNKNOWN = "unknown"


# Mapping from persona name → identity class.
#
# Keys must match ``BUILTIN_PERSONAS`` in ``persona.py``; a meta-test
# (``test_persona_identity_map_covers_all_pr_personas``) enforces that
# every persona whose ``tools`` list contains a GitHub-touching tool is
# either in this map or explicitly opted out via the legacy fallback.
#
# Personas not listed here fall through to ``GITHUB_TOKEN`` (legacy).
PERSONA_IDENTITY_MAP: dict[str, str] = {
    # Builder personas — open and update PRs.
    "alfred-coo-a": GitHubIdentityClass.BUILDER,
    "alfred-coo": GitHubIdentityClass.BUILDER,  # legacy alias
    "riddler-crypto-a": GitHubIdentityClass.BUILDER,
    # QA / reviewer personas — submit pr_review verdicts.
    "hawkman-qa-a": GitHubIdentityClass.QA,
    "batgirl-sec-a": GitHubIdentityClass.QA,
    # Orchestrator — merges and runs hint-verification probes.
    "autonomous-build-a": GitHubIdentityClass.ORCHESTRATOR,
}


# ── Persona context (parallel to tools._current_task_id) ────────────────────

_current_persona: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "alfred_coo_current_persona", default=None
)


def get_current_persona() -> Optional[str]:
    """Return the persona name set by the dispatch loop, or None if
    no dispatch is active (ad-hoc CLI invocation, tests without setup,
    etc.)."""
    return _current_persona.get()


def set_current_persona(name: Optional[str]):
    """Set the active persona for the duration of a tool-use call.

    Returns a token for use with ``reset_current_persona``. The
    dispatch loop in ``main.py`` calls this around
    ``call_with_tools`` so handlers see the right persona without the
    model having to pass it as a parameter.
    """
    return _current_persona.set(name)


def reset_current_persona(token) -> None:
    _current_persona.reset(token)


# ── Token resolver ──────────────────────────────────────────────────────────

# Class → env-var name. Centralised so tests + the resolver agree on
# the contract.
_TOKEN_ENV_VARS: dict[str, str] = {
    GitHubIdentityClass.BUILDER: "GITHUB_TOKEN_BUILDER",
    GitHubIdentityClass.QA: "GITHUB_TOKEN_QA",
    GitHubIdentityClass.ORCHESTRATOR: "GITHUB_TOKEN_ORCHESTRATOR",
}

_LOGIN_ENV_VARS: dict[str, str] = {
    GitHubIdentityClass.BUILDER: "GITHUB_LOGIN_BUILDER",
    GitHubIdentityClass.QA: "GITHUB_LOGIN_QA",
    GitHubIdentityClass.ORCHESTRATOR: "GITHUB_LOGIN_ORCHESTRATOR",
}

_LEGACY_TOKEN_ENV_VAR = "GITHUB_TOKEN"


def identity_class_for_persona(persona_name: Optional[str]) -> str:
    """Return the ``GitHubIdentityClass`` value for a persona name.

    Unknown / None / empty → ``UNKNOWN``. Caller is responsible for
    falling back to legacy behaviour on UNKNOWN.
    """
    if not persona_name:
        return GitHubIdentityClass.UNKNOWN
    return PERSONA_IDENTITY_MAP.get(persona_name, GitHubIdentityClass.UNKNOWN)


def token_for_persona(persona_name: Optional[str]) -> tuple[str, str]:
    """Resolve (token, identity_class) for a persona.

    Lookup chain:
      1. ``persona_name`` → identity class (UNKNOWN if not mapped).
      2. If the class is BUILDER / QA / ORCHESTRATOR, try the matching
         per-class env var (``GITHUB_TOKEN_BUILDER`` etc.).
      3. ORCHESTRATOR has an extra fallback to ``GITHUB_TOKEN_QA`` —
         "QA approved → QA merges" is the cleanest semantic when no
         dedicated orchestrator bot exists.
      4. Anything still unset falls through to legacy ``GITHUB_TOKEN``.
      5. If even the legacy var is missing, return ``("", "unknown")``
         so the caller's existing missing-token error path fires.

    Returns the token string and the *resolved* class label (which may
    differ from the requested class if a fallback fired) so the caller
    can log / route accordingly.
    """
    cls = identity_class_for_persona(persona_name)

    # Step 1: try the dedicated env var for the persona's class.
    env_var = _TOKEN_ENV_VARS.get(cls)
    if env_var:
        token = os.environ.get(env_var, "").strip()
        if token:
            return token, cls

    # Step 2: orchestrator-specific fallback chain — try QA before
    # legacy. Rationale in the design doc §4.4.
    if cls == GitHubIdentityClass.ORCHESTRATOR:
        qa_token = os.environ.get(_TOKEN_ENV_VARS[GitHubIdentityClass.QA], "").strip()
        if qa_token:
            return qa_token, GitHubIdentityClass.QA

    # Step 3: legacy ``GITHUB_TOKEN`` catch-all.
    legacy = os.environ.get(_LEGACY_TOKEN_ENV_VAR, "").strip()
    if legacy:
        return legacy, GitHubIdentityClass.UNKNOWN

    # Step 4: nothing configured.
    return "", GitHubIdentityClass.UNKNOWN


def login_for_class(identity_class: str) -> Optional[str]:
    """Return the configured login for an identity class, or None.

    Used for diagnostic logging at startup and for the future
    PR-author-comparison check in hawkman. None when the operator has
    not declared the login (login-aware features degrade silently to
    today's 422-trigger path).
    """
    var = _LOGIN_ENV_VARS.get(identity_class)
    if not var:
        return None
    val = os.environ.get(var, "").strip()
    return val or None


def log_identity_summary() -> None:
    """Emit a single INFO line summarising the configured identities.

    Called once on daemon startup so operators can verify split-identity
    is in effect without grepping deeper logs. Safe to call any time;
    no side effects beyond the log line.
    """
    bits: list[str] = []
    legacy_set = bool(os.environ.get(_LEGACY_TOKEN_ENV_VAR, "").strip())
    for cls in (
        GitHubIdentityClass.BUILDER,
        GitHubIdentityClass.QA,
        GitHubIdentityClass.ORCHESTRATOR,
    ):
        env_var = _TOKEN_ENV_VARS[cls]
        has_token = bool(os.environ.get(env_var, "").strip())
        login = login_for_class(cls) or "(unset)"
        if has_token:
            bits.append(f"{cls}={login}")
        elif legacy_set:
            bits.append(f"{cls}=(legacy)")
        else:
            bits.append(f"{cls}=(missing)")

    summary = " ".join(bits)
    if not legacy_set and not any(
        os.environ.get(v, "").strip() for v in _TOKEN_ENV_VARS.values()
    ):
        logger.warning(
            "github_identity: no tokens configured; GitHub-touching tools "
            "will return 'GITHUB_TOKEN not configured' errors"
        )
        return

    # Single-token mode — flag the self-authored-fallback risk explicitly.
    if legacy_set and not any(
        os.environ.get(v, "").strip() for v in _TOKEN_ENV_VARS.values()
    ):
        logger.info(
            "github_identity: single-token mode (only GITHUB_TOKEN set); "
            "self-authored fallback risk applies — set GITHUB_TOKEN_QA to "
            "split builder/reviewer identities. resolved=%s",
            summary,
        )
        return

    logger.info("github_identity: %s", summary)
