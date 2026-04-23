"""Autonomous build orchestrator package.

Long-running program controller for Mission Control v1.0 GA. Claims one
kickoff mesh task, then dispatches per-ticket child tasks in waves until
the whole Linear project reaches all-green.

Plan: Z:/_planning/v1-ga/F_autonomous_build_persona.md

The package is deliberately small:
- `graph.py`      — Linear -> TicketGraph loader
- `state.py`      — soul-memory checkpoint/restore
- `orchestrator.py` — wave scheduler + dep resolver

AB-05 will add `budget.py` + Slack cadence; AB-06 fills in the SS-08 gate.
AB-07 bolts on dry-run mode. AB-04 (this ticket) ships the core with
stub hooks for the later tickets.
"""

from .orchestrator import AutonomousBuildOrchestrator

__all__ = ["AutonomousBuildOrchestrator"]
