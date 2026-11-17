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
