"""Path-safe artifact writer for Alfred COO daemon.

Writes artifacts emitted by a structured-output model response to
/var/lib/alfred-coo/artifacts/<task_id>/<rel-path>. Rejects anything that
would escape the task workspace (absolute paths, `..` segments, symlink
traversal via intermediate components).

The writer creates the task workspace lazily and returns the list of absolute
paths actually written so the caller can attach them to the mesh complete
payload.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable, List, Mapping


logger = logging.getLogger("alfred_coo.artifacts")

DEFAULT_ARTIFACT_ROOT = Path("/var/lib/alfred-coo/artifacts")


def _safe_relative(path: str) -> Path | None:
    """Return a Path if `path` is a safe relative path, else None.

    Rejects absolute paths, drive-letter paths, `..` segments, and the
    literal current-directory segment used by itself.
    """
    if not path or not isinstance(path, str):
        return None
    p = path.strip().replace("\\", "/")
    if not p or p.startswith("/") or (len(p) >= 2 and p[1] == ":"):
        return None
    parts = [seg for seg in p.split("/") if seg and seg != "."]
    if not parts:
        return None
    for seg in parts:
        if seg == ".." or seg.startswith("\0"):
            return None
    return Path(*parts)


def write_artifacts(
    task_id: str,
    artifacts: Iterable[Mapping[str, str]],
    root: Path | None = None,
) -> List[str]:
    """Write each artifact to <root>/<task_id>/<path>. Returns written paths.

    Unsafe paths are logged and skipped. Write errors are logged and skipped;
    a single bad artifact must not abort the rest.
    """
    if root is None:
        root = DEFAULT_ARTIFACT_ROOT
    workspace = root / task_id
    workspace.mkdir(parents=True, exist_ok=True)

    # Resolve once so the containment check survives symlinks already in place.
    workspace_resolved = workspace.resolve()

    written: List[str] = []
    for a in artifacts:
        rel = _safe_relative(a.get("path", "") or "")
        if rel is None:
            logger.warning("artifact skipped — unsafe path: %r", a.get("path"))
            continue
        target = workspace / rel
        try:
            # Resolve with strict=False so we can check intent before creating
            # intermediate dirs. Then compare against the workspace root.
            intended = target.resolve()
            try:
                intended.relative_to(workspace_resolved)
            except ValueError:
                logger.warning(
                    "artifact skipped — would escape workspace: %r", str(target)
                )
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            content = a.get("content", "") or ""
            target.write_text(content, encoding="utf-8", newline="\n")
            written.append(str(target))
        except Exception:
            logger.exception("artifact write failed for %r", a.get("path"))
            continue

    if written:
        logger.info(
            "wrote %d artifact(s) for task %s under %s",
            len(written),
            task_id,
            workspace,
        )
    return written
