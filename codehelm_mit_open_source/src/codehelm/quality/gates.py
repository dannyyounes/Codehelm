"""Fail-closed validation and independent AI review gates."""
from dataclasses import dataclass
from pathlib import Path
import subprocess

from codehelm.providers.adapters import CLIProvider


@dataclass(frozen=True)
class GateResult:
    passed: bool
    detail: str


def command_gate(workspace: Path, command: list[str], timeout: int = 300) -> GateResult:
    """Execute an explicit argv command; no shell interpolation."""
    if not command:
        return GateResult(False, "No command configured")
    try:
        result = subprocess.run(command, cwd=workspace, capture_output=True,
                                text=True, timeout=timeout, shell=False)
        return GateResult(result.returncode == 0,
                          (result.stdout + "\n" + result.stderr)[-4000:])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GateResult(False, str(exc))


def git_snapshot(workspace: Path) -> str:
    """Capture tracked and untracked changes; review must not modify workspace."""
    status = subprocess.run(["git", "status", "--porcelain", "-uall"], cwd=workspace,
                            capture_output=True, text=True, check=True)
    diff = subprocess.run(["git", "diff", "HEAD", "--binary"], cwd=workspace,
                          capture_output=True, text=True, check=True)
    return status.stdout + "\n" + diff.stdout


def independent_review(workspace: Path, author: str, reviewer: str,
                       timeout: int = 600) -> GateResult:
    if reviewer == author:
        return GateResult(False, "Reviewer must use a different provider from the author")
    before = git_snapshot(workspace)
    if not before.strip():
        return GateResult(False, "No changes to review")
    prompt = ("Review the changes in this repository independently for correctness, "
              "maintainability, security and tests. Do not edit files. "
              "Return a final line exactly APPROVED or CHANGES_REQUESTED, "
              "followed by no other text. Explain findings before that line.")
    result = CLIProvider(reviewer).execute(prompt, workspace, timeout)
    after = git_snapshot(workspace)
    if after != before:
        return GateResult(False, "Reviewer modified the workspace; review invalid")
    if result.exit_code:
        return GateResult(False, f"Reviewer failed: {result.error[-2000:]}")
    lines = result.output.strip().splitlines()
    approved = bool(lines and lines[-1].strip() == "APPROVED")
    return GateResult(approved, result.output[-4000:] or "Reviewer returned no verdict")
