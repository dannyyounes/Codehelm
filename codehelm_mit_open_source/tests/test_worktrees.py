from pathlib import Path
import subprocess
import pytest
from codehelm.execution.worktrees import WorktreeManager, WorktreeError


def git(path: Path, *args: str):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init")
    git(path, "config", "user.email", "test@example.org")
    git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello")
    git(path, "add", ".")
    git(path, "commit", "-m", "initial")
    return path


def test_create_and_remove(repo):
    manager = WorktreeManager(repo)
    wt = manager.create("DEV-123")
    assert (wt.path / "README.md").exists()
    assert "codehelm/DEV-123" in manager.list()
    manager.remove("DEV-123")
    assert not wt.path.exists()
    # Branch intentionally preserved.
    git(repo, "show-ref", "--verify", "refs/heads/codehelm/DEV-123")


def test_reject_invalid_and_duplicate(repo):
    manager = WorktreeManager(repo)
    with pytest.raises(WorktreeError):
        manager.create("../escape")
    manager.create("DEV-1")
    with pytest.raises(WorktreeError):
        manager.create("DEV-1")


def test_refuse_dirty_remove(repo):
    manager = WorktreeManager(repo)
    wt = manager.create("DEV-2")
    (wt.path / "new.txt").write_text("untracked")
    with pytest.raises(WorktreeError):
        manager.remove("DEV-2")
    assert wt.path.exists()
