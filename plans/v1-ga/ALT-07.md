# ALT-07: Mesh task-completion subscriber

## Target paths
- aletheia/app/watchers/mesh_subscriber.py
- aletheia/tests/test_mesh_subscriber.py
- plans/v1-ga/ALT-07.md

## Acceptance criteria
- fire `mcp__alfred__mesh_task_complete` with known id
- within 10s verdict record written
- `soul_memory_search` finds it

## Verification approach
- Run the unit test suite (`pytest -q`). The test ensures the subscriber enqueues a verification job when handling an event with an ``id``.
- Trigger a real mesh task completion event in a staging environment and confirm:
  1. The ``mcp__alfred__mesh_task_complete`` hook fires with the expected task identifier.
  2. A verdict record appears in ``soul_memory`` within 10 seconds.
  3. A subsequent ``soul_memory_search`` query returns the newly written verdict.

## Risks
- Race condition: the verification job may not complete before the 10‑second window; mitigate by ensuring the queue worker pool has sufficient capacity.
- Dependency on Redis availability: the subscriber enqueues to ``task_queue`` which uses Redis; a Redis outage would cause loss of verification jobs.
- Incorrect import paths if package layout changes; keep the watcher registration in sync with the service's startup configuration.
