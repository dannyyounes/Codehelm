"""Fail-closed validation and independent AI review gates."""
from dataclasses import dataclass
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys

from codehelm.providers.adapters import CLIProvider

_SANDBOX_ENV = {
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM",
    "PYTHONPATH", "PYTHONHOME", "PYTHONIOENCODING", "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE",
}
_SECRET_ENV = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE|AWS_|GITHUB_|GH_|CODEHELM_)",
    re.IGNORECASE)


@dataclass(frozen=True)
class GateResult:
    passed: bool
    detail: str
    exit_code: int | None = None


def _sandbox_env() -> dict[str, str]:
    """Keep the interpreter's search path and drop credential variables."""
    return {
        key: value for key, value in os.environ.items()
        if key in _SANDBOX_ENV and not _SECRET_ENV.search(key)
    }


def command_gate(workspace: Path, command: list[str], timeout: int = 300) -> GateResult:
    """Run an argv command in a namespace sandbox. No shell, and no unsandboxed fallback."""
    if not command:
        return GateResult(False, "No command configured")
    if any(not isinstance(part, str) or "\x00" in part or part == "" and index == 0
           for index, part in enumerate(command)):
        return GateResult(False, "Command arguments must be strings without NULs")
    if shutil.which("unshare") is None:
        return GateResult(False, "Sandbox unavailable: unshare is not installed")
    if not Path(workspace).is_dir():
        return GateResult(False, "Workspace missing")
    try:
        result = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--mount", "--propagation", "private",
             "--pid", "--fork", "--net", "--mount-proc", "--",
             sys.executable, "-m", "codehelm.quality.sandbox", str(Path(workspace).resolve()),
             "--", *command],
            cwd=workspace, env=_sandbox_env(), capture_output=True,
            text=True, timeout=timeout, shell=False)
        return GateResult(result.returncode == 0,
                          (result.stdout + "\n" + result.stderr)[-4000:],
                          result.returncode)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GateResult(False, str(exc))


def git_snapshot(workspace: Path) -> str:
    """Capture tracked and untracked changes; review must not modify workspace."""
    status = subprocess.run(["git", "status", "--porcelain", "-uall"], cwd=workspace,
                            capture_output=True, text=True, check=True)
    diff = subprocess.run(["git", "diff", "HEAD", "--binary"], cwd=workspace,
                          capture_output=True, text=True, check=True)
    return status.stdout + "\n" + diff.stdout


_REVIEW_PROMPTS = {
    "code": ("Review the changes in this repository independently for correctness, "
             "maintainability, security and tests. Do not edit files. "
             "Return a final line exactly APPROVED or CHANGES_REQUESTED, "
             "followed by no other text. Explain findings before that line."),
    "architecture": ("Review the architecture of these changes for boundaries, data "
                     "model, security and compatibility. Do not edit files. "
                     "Return a final line exactly APPROVED or CHANGES_REQUESTED, "
                     "followed by no other text. Explain findings before that line."),
}


def independent_review(workspace: Path, author: str, reviewer: str,
                       timeout: int = 600, *, kind: str = "code") -> GateResult:
    """Model verdict plus a Git snapshot guard. This is not human approval."""
    if kind not in _REVIEW_PROMPTS:
        return GateResult(False, f"Unknown review kind: {kind}")
    if reviewer == author:
        return GateResult(False, "Reviewer must use a different provider from the author")
    before = git_snapshot(workspace)
    if not before.strip():
        return GateResult(False, "No changes to review")
    result = CLIProvider(reviewer).execute(_REVIEW_PROMPTS[kind], workspace, timeout)
    after = git_snapshot(workspace)
    if after != before:
        return GateResult(False, "Reviewer modified the workspace; review invalid", result.exit_code)
    if result.exit_code:
        return GateResult(False, f"Reviewer failed: {result.error[-2000:]}", result.exit_code)
    lines = result.output.strip().splitlines()
    approved = bool(lines and lines[-1].strip() == "APPROVED")
    return GateResult(approved, result.output[-4000:] or "Reviewer returned no verdict",
                      result.exit_code)
