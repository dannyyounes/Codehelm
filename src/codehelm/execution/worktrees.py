"""Git worktree isolation for coding agents (no model calls)."""
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess


class WorktreeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Worktree:
    task_id: str
    branch: str
    path: Path


class WorktreeManager:
    def __init__(self, repository: Path, workspace_root: Path | None = None):
        self.repository = Path(repository).resolve()
        self.workspace_root = (Path(workspace_root).resolve() if workspace_root
                               else self.repository.parent / f"{self.repository.name}-worktrees")
        self._git("rev-parse", "--show-toplevel")

    def _git(self, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(self.repository), *args],
                                capture_output=True, text=True, check=False)
        if result.returncode:
            raise WorktreeError(result.stderr.strip() or result.stdout.strip())
        return result.stdout.strip()

    def _identity(self, task_id: str) -> Worktree:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", task_id):
            raise WorktreeError("Task ID must be 1-80 letters, digits, underscores or hyphens")
        return Worktree(task_id, f"codehelm/{task_id}", self.workspace_root / task_id)

    def list(self) -> str:
        return self._git("worktree", "list", "--porcelain")

    def create(self, task_id: str, base: str = "HEAD") -> Worktree:
        worktree = self._identity(task_id)
        if worktree.path.exists():
            raise WorktreeError(f"Workspace already exists: {worktree.path}")
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        # Never overwrite an existing branch. Git rejects duplicate branch names.
        self._git("worktree", "add", "-b", worktree.branch, str(worktree.path), base)
        return worktree

    def remove(self, task_id: str) -> None:
        worktree = self._identity(task_id)
        registered = self.list()
        if f"worktree {worktree.path}\n" not in registered + "\n":
            raise WorktreeError("Workspace is not registered with this repository")
        # Git refuses to remove a dirty worktree without --force.
        self._git("worktree", "remove", str(worktree.path))
        # Preserve branch for recovery and review.
