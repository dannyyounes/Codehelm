"""Repository hygiene: no bytecode should be tracked in git."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _is_git_checkout(path: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def test_no_tracked_pyc_files():
    if not _is_git_checkout(REPO_ROOT):
        pytest.skip("not a git checkout")
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "--", "*.pyc"],
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = [line for line in result.stdout.splitlines() if line.strip()]
    assert tracked == [], f"tracked *.pyc files must be untracked: {tracked}"


def test_local_plan_files_are_gitignored():
    """One-off `.codehelm/plan-*.json` files are ignored. Tracked files stay tracked."""
    pattern = ".codehelm/plan-*.json"
    assert pattern in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    if not _is_git_checkout(REPO_ROOT):
        pytest.skip("not a git checkout")
    ignored = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-v", "--", ".codehelm/plan-local.json"],
        capture_output=True, text=True, check=False)
    assert ignored.returncode == 0 and pattern in ignored.stdout
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "--error-unmatch", "--", "README.md"],
        capture_output=True, text=True, check=False)
    assert tracked.returncode == 0
    still = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "--", "README.md"],
        capture_output=True, text=True, check=False)
    assert still.returncode != 0
