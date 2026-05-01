"""Doctor playbook registry (Phase 2).

Each playbook is a bounded autonomous repair action invoked once per
doctor tick. ``DEFAULT_PLAYBOOKS`` is what the doctor walks at runtime
and is also the public surface area for tests.

Adding a playbook:
1. Subclass ``Playbook`` from ``base``, implement ``execute``.
2. Append an instance to ``DEFAULT_PLAYBOOKS`` below.
3. Add tests covering scan + dry-run + wet-run + error paths.
"""

from .base import Playbook, PlaybookResult
from .hydrate_apev import HydrateAPEVHeadingsPlaybook
from .refresh_dashboard_next_gate import RefreshDashboardNextGatePlaybook


DEFAULT_PLAYBOOKS: list[Playbook] = [
    HydrateAPEVHeadingsPlaybook(),
    RefreshDashboardNextGatePlaybook(),
]


__all__ = [
    "Playbook",
    "PlaybookResult",
    "HydrateAPEVHeadingsPlaybook",
    "RefreshDashboardNextGatePlaybook",
    "DEFAULT_PLAYBOOKS",
]
