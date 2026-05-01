"""Base classes for alfred-doctor playbooks (Phase 2).

A playbook is a bounded autonomous repair action invoked once per doctor
tick after the surveillance scan. Each playbook owns its own targeted
scan + mutation logic; the doctor merely invokes them and folds their
PlaybookResult into the Slack digest.

Safety contract every playbook must honor:
* **Idempotent.** Re-running on the same Linear/mesh state must not
  double-act. Playbooks check for the presence of their own past
  effect (e.g. canonical APE/V heading) before mutating.
* **Bounded.** ``max_actions_per_tick`` caps writes per tick so a
  runaway data shape can't drain Linear rate limits or fan-out badly.
* **Dry-run aware.** When ``dry_run=True`` the playbook MUST NOT call
  any mutating API. It only counts what it *would* do.
* **Loud-on-error.** Errors land in ``PlaybookResult.errors``; the
  doctor chain MUST keep running. A playbook crash never breaks the
  surveillance loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlaybookResult:
    """Per-tick report from one playbook.

    Rendered into the Slack digest by ``render_digest_lines``. The doctor
    also persists ``counters`` summaries via the mesh task result envelope
    so future ticks can reason about playbook activity history.
    """

    kind: str
    candidates_found: int = 0
    actions_taken: int = 0
    actions_skipped: int = 0
    dry_run: bool = True
    notable: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def is_silent(self) -> bool:
        """True iff this playbook had nothing to say this tick.

        Used by the digest renderer to decide whether to emit any
        playbook lines at all (a chain of all-silent playbooks should
        not noise up the digest)."""
        return (
            self.candidates_found == 0
            and self.actions_taken == 0
            and self.actions_skipped == 0
            and not self.errors
        )

    def render_digest_lines(self) -> list[str]:
        """Render this playbook's contribution to the Slack digest.

        Returns an empty list when the playbook is silent so the caller
        can simply ``extend`` and skip empty playbooks transparently."""
        if self.is_silent():
            return []
        prefix = "[dry] " if self.dry_run else ""
        head = (
            f"  {prefix}{self.kind}: found={self.candidates_found}"
            f" acted={self.actions_taken}"
        )
        if self.actions_skipped:
            head += f" skipped={self.actions_skipped}"
        if self.errors:
            head += f" errors={len(self.errors)}"
        lines = [head]
        for n in self.notable[:5]:
            lines.append(f"    · {n}")
        for e in self.errors[:3]:
            lines.append(f"    ! {e}")
        return lines


class Playbook:
    """Base class for a doctor playbook.

    Subclasses override ``execute`` to do their work. The doctor invokes
    one playbook per registered class per tick; ordering is by registry
    insertion (see ``playbooks/__init__.py``).
    """

    kind: str = "unknown"
    max_actions_per_tick: int = 5

    async def execute(
        self,
        *,
        linear_api_key: str,
        dry_run: bool,
    ) -> PlaybookResult:
        raise NotImplementedError(
            "Playbook subclasses must implement execute()"
        )
