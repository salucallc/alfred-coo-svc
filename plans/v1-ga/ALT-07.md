# ALT-07: Mesh task-completion subscriber

## Target paths
- aletheia/app/watchers/mesh_subscriber.py
- aletheia/tests/test_mesh_subscriber.py

## Acceptance criteria
- Fire `mcp__alfred__mesh_task_complete` with known id; within 10s verdict record written. `mcp__alfred__soul_memory_search` finds it.

## Verification approach
- Unit test `test_mesh_subscriber_enqueues_job` uses a mock Redis client to verify that handling a mesh task complete event pushes a job onto the `aletheia:pending` queue.
- Integration test runs the subscriber against a test Redis instance and checks that a verdict record appears via `mcp__alfred__soul_memory_search` within 10 seconds.

## Risks
- Potential race condition if Redis is unavailable; subscriber logs error and drops event.
- Ensure no deletion of existing service files; only new files added.
