"""GitHub pull request publishing through authenticated gh CLI."""
from dataclasses import dataclass
from pathlib import Path
import json
import shutil
import subprocess


class PullRequestError(RuntimeError):
    pass


@dataclass(frozen=True)
class PullRequest:
    url: str
    branch: str


def _run(argv: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                                timeout=120, check=False, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PullRequestError(str(exc)) from exc
    if result.returncode:
        raise PullRequestError((result.stderr or result.stdout).strip()[:1000])
    return result.stdout.strip()


def publish_pr(workspace: Path, branch: str, title: str, body: str,
               base: str = "main", remote: str = "origin") -> PullRequest:
    """Push an existing committed branch and create a draft PR; never merge."""
    workspace = Path(workspace).resolve()
    if not workspace.is_dir():
        raise PullRequestError("Workspace does not exist")
    if not shutil.which("gh"):
        raise PullRequestError("GitHub CLI (gh) is not installed")
    if _run(["git", "status", "--porcelain", "-uall"], workspace):
        raise PullRequestError("Workspace has uncommitted or untracked files; commit first")
    current = _run(["git", "branch", "--show-current"], workspace)
    if current != branch:
        raise PullRequestError(f"Expected branch {branch}, got {current}")
    if not title.strip():
        raise PullRequestError("PR title cannot be empty")
    if not _run(["git", "log", "-1", "--format=%H"], workspace):
        raise PullRequestError("No commit found")
    _run(["gh", "auth", "status"], workspace)
    # Network write occurs only after caller has explicitly requested execution.
    _run(["git", "push", "-u", remote, branch], workspace)
    output = _run(["gh", "pr", "create", "--draft", "--base", base,
                   "--head", branch, "--title", title, "--body", body], workspace)
    url = output.splitlines()[-1].strip()
    if not url.startswith("https://github.com/"):
        raise PullRequestError("PR command did not return a GitHub URL")
    return PullRequest(url=url, branch=branch)
