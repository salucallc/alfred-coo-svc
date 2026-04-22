"""Artifact writer path-safety tests."""

from pathlib import Path

from alfred_coo.artifacts import write_artifacts, _safe_relative


def test_safe_relative_accepts_plain():
    assert _safe_relative("a.md") == Path("a.md")
    assert _safe_relative("sub/b.py") == Path("sub") / "b.py"


def test_safe_relative_rejects_absolute():
    assert _safe_relative("/etc/passwd") is None


def test_safe_relative_rejects_drive():
    assert _safe_relative("C:/Windows/System32/x") is None


def test_safe_relative_rejects_parent_segment():
    assert _safe_relative("../../../etc/passwd") is None
    assert _safe_relative("a/../../b") is None


def test_safe_relative_accepts_dot_segments():
    # A leading ./ is fine; it just means the same dir.
    assert _safe_relative("./a.md") == Path("a.md")


def test_safe_relative_rejects_empty():
    assert _safe_relative("") is None
    assert _safe_relative("   ") is None
    assert _safe_relative(None) is None  # type: ignore[arg-type]


def test_write_artifacts_writes_expected_files(tmp_path: Path):
    artifacts = [
        {"path": "notes.md", "content": "# hello\n"},
        {"path": "sub/inner.txt", "content": "deep"},
    ]
    written = write_artifacts("task-abc", artifacts, root=tmp_path)
    assert len(written) == 2
    assert (tmp_path / "task-abc" / "notes.md").read_text() == "# hello\n"
    assert (tmp_path / "task-abc" / "sub" / "inner.txt").read_text() == "deep"


def test_write_artifacts_skips_unsafe(tmp_path: Path):
    artifacts = [
        {"path": "ok.md", "content": "fine"},
        {"path": "../escape.md", "content": "nope"},
        {"path": "/absolute.md", "content": "also nope"},
    ]
    written = write_artifacts("task-xyz", artifacts, root=tmp_path)
    assert len(written) == 1
    assert (tmp_path / "task-xyz" / "ok.md").exists()
    # Escape attempts must not have created any file outside the workspace.
    assert not (tmp_path / "escape.md").exists()
    assert not (tmp_path / "absolute.md").exists()


def test_write_artifacts_handles_empty_list(tmp_path: Path):
    written = write_artifacts("task-empty", [], root=tmp_path)
    assert written == []
    # Workspace dir is still created lazily (harmless).
    assert (tmp_path / "task-empty").is_dir()
