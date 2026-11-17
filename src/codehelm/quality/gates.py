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


_RUNNERS = frozenset({"namespace", "container"})


def _run_argv(argv: list[str], workspace: Path, timeout: int) -> GateResult:
    """Run argv with the credential environment removed. Never through a shell."""
    try:
        result = subprocess.run(
            argv, cwd=workspace, env=_sandbox_env(), capture_output=True,
            text=True, timeout=timeout, shell=False)
        return GateResult(result.returncode == 0,
                          (result.stdout + "\n" + result.stderr)[-4000:],
                          result.returncode)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GateResult(False, str(exc))


def _namespace_gate(workspace: Path, command: list[str], timeout: int) -> GateResult:
    """Existing unshare sandbox. A missing unshare does not run the command on the host."""
    if shutil.which("unshare") is None:
        return GateResult(False, "Sandbox unavailable: unshare is not installed")
    if not Path(workspace).is_dir():
        return GateResult(False, "Workspace missing")
    return _run_argv(
        ["unshare", "--user", "--map-root-user", "--mount", "--propagation", "private",
         "--pid", "--fork", "--net", "--mount-proc", "--",
         sys.executable, "-m", "codehelm.quality.sandbox", str(Path(workspace).resolve()),
         "--", *command],
        workspace, timeout)


def _container_runtime() -> str | None:
    """podman first, then docker. Neither means the gate cannot run."""
    for name in ("podman", "docker"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _container_gate(workspace: Path, command: list[str], timeout: int, image: str) -> GateResult:
    """Run the command only through a local container runtime. No image pull and no unshare fallback."""
    if not isinstance(image, str) or not image.strip() or "\x00" in image:
        return GateResult(False, "Container runner requires gate_image")
    image = image.strip()
    if image.startswith("-") or any(character.isspace() for character in image):
        return GateResult(False, "Container runner requires a local image name")
    if not Path(workspace).is_dir():
        return GateResult(False, "Workspace missing")
    runtime = _container_runtime()
    if runtime is None:
        return GateResult(False, "Container runner unavailable: podman and docker are not installed")
    resolved = Path(workspace).resolve()
    # --pull=never refuses a registry fetch. The command follows the image as argv.
    argv = [
        runtime, "run", "--rm", "--network=none", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--pull=never",
        "--volume", f"{resolved}:{resolved}",
        "--workdir", str(resolved),
        image,
        *command,
    ]
    return _run_argv(argv, workspace, timeout)


def command_gate(workspace: Path, command: list[str], timeout: int = 300, *,
                 runner: str = "namespace", image: str = "") -> GateResult:
    """Run an argv command in the selected sandbox. No shell, and no unsandboxed fallback."""
    if not command:
        return GateResult(False, "No command configured")
    if any(not isinstance(part, str) or "\x00" in part or part == "" and index == 0
           for index, part in enumerate(command)):
        return GateResult(False, "Command arguments must be strings without NULs")
    if runner not in _RUNNERS:
        return GateResult(False, "Unknown gate runner")
    if runner == "container":
        return _container_gate(workspace, command, timeout, image)
    return _namespace_gate(workspace, command, timeout)


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

_STORY_CONTEXT_LIMIT = 12000
_STORY_REVIEW_INSTRUCTION = (
    "Judge the change against the story and acceptance criteria. "
    "Report real defects: correctness, regressions, security issues within the declared scope, "
    "and missing tests for the criteria. Do not request hardening for cases the story declares out of scope. "
    "If remaining concerns are only out-of-scope or optional improvements, list them as notes and still return APPROVED."
)


def _review_prompt(kind: str, story_title: str | None, story_description: str | None,
                   acceptance_criteria: list[str] | tuple[str, ...] | None,
                   scope_text: str | None) -> str:
    base = _REVIEW_PROMPTS[kind]
    if (story_title is None and story_description is None
            and acceptance_criteria is None and scope_text is None):
        return base
    criteria = "\n".join(f"- {item}" for item in (acceptance_criteria or ()))
    lines = ["Story", story_title or "", story_description or "", "", "Acceptance criteria", criteria]
    if scope_text is not None:
        lines += ["", "Declared scope and threat model", scope_text]
    context = "\n".join(lines)
    if len(context) > _STORY_CONTEXT_LIMIT:
        context = context[:_STORY_CONTEXT_LIMIT]
    return f"{context}\n\n{_STORY_REVIEW_INSTRUCTION}\n\n{base}"


def independent_review(workspace: Path, author: str, reviewer: str,
                       timeout: int = 600, *, kind: str = "code",
                       story_title: str | None = None,
                       story_description: str | None = None,
                       acceptance_criteria: list[str] | tuple[str, ...] | None = None,
                       scope_text: str | None = None) -> GateResult:
    """Model verdict plus a Git snapshot guard. This is not human approval."""
    if kind not in _REVIEW_PROMPTS:
        return GateResult(False, f"Unknown review kind: {kind}")
    if reviewer == author:
        return GateResult(False, "Reviewer must use a different provider from the author")
    before = git_snapshot(workspace)
    if not before.strip():
        return GateResult(False, "No changes to review")
    prompt = _review_prompt(kind, story_title, story_description, acceptance_criteria, scope_text)
    result = CLIProvider(reviewer).execute(prompt, workspace, timeout)
    after = git_snapshot(workspace)
    if after != before:
        return GateResult(False, "Reviewer modified the workspace; review invalid", result.exit_code)
    if result.exit_code:
        return GateResult(False, f"Reviewer failed: {result.error[-2000:]}", result.exit_code)
    lines = result.output.strip().splitlines()
    approved = bool(lines and lines[-1].strip() == "APPROVED")
    return GateResult(approved, result.output[-4000:] or "Reviewer returned no verdict",
                      result.exit_code)
