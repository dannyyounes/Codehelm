"""GitHub pull request publishing and the authenticated gh user."""
from dataclasses import dataclass
from pathlib import Path
import json
import re
import shutil
import subprocess

_GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")


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


_PR_URL = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*$")
_HEAD_OID = re.compile(r"^[0-9a-fA-F]{40}$")


def _pull_request_url(url: str) -> str:
    url = (url or "").strip()
    if not _PR_URL.fullmatch(url):
        raise PullRequestError("Stored PR URL is not a GitHub pull request")
    return url


def _check_succeeded(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    kind = item.get("__typename")
    if kind == "CheckRun" or (kind is None and "conclusion" in item):
        return item.get("status") == "COMPLETED" and item.get("conclusion") == "SUCCESS"
    if kind == "StatusContext" or (kind is None and "state" in item and "conclusion" not in item):
        return item.get("state") == "SUCCESS"
    return False


def pull_request_head_if_green(payload: object) -> str:
    """Return the head SHA when the pull request is open and every check succeeded."""
    if not isinstance(payload, dict):
        raise PullRequestError("GitHub did not return pull request JSON")
    if payload.get("state") != "OPEN":
        raise PullRequestError(f"Pull request is not open (state={payload.get('state')})")
    oid = payload.get("headRefOid")
    if not isinstance(oid, str) or not _HEAD_OID.fullmatch(oid):
        raise PullRequestError("Pull request head is not a commit SHA")
    rollup = payload.get("statusCheckRollup")
    if not isinstance(rollup, list) or not rollup:
        raise PullRequestError("CI has no successful checks")
    if not all(_check_succeeded(item) for item in rollup):
        raise PullRequestError("CI is not green")
    return oid


def github_pull_request_green(url: str) -> str:
    """Return the head SHA when gh reports the pull request open and every check success."""
    url = _pull_request_url(url)
    if not shutil.which("gh"):
        raise PullRequestError("GitHub CLI (gh) is not installed")
    raw = _run(["gh", "pr", "view", url, "--json", "state,statusCheckRollup,headRefOid"], Path.cwd())
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PullRequestError("GitHub did not return pull request JSON") from exc
    return pull_request_head_if_green(payload)


def github_merge_pull_request(url: str, head_oid: str) -> None:
    """Merge now with gh. Does not pass --auto or --admin, and requires a merged result.

    A command that only arms GitHub auto-merge leaves the pull request open, so
    this raises. It does not disable that flag and does not complete a story.
    """
    url = _pull_request_url(url)
    if not isinstance(head_oid, str) or not _HEAD_OID.fullmatch(head_oid):
        raise PullRequestError("Pull request head is not a commit SHA")
    if not shutil.which("gh"):
        raise PullRequestError("GitHub CLI (gh) is not installed")
    _run(["gh", "pr", "merge", url, "--merge", "--match-head-commit", head_oid], Path.cwd())
    try:
        github_pull_request_merged(url)
    except PullRequestError as exc:
        raise PullRequestError("Pull request was not merged") from exc


def github_pull_request_merged(url: str) -> None:
    """Raise unless gh reports this pull request merged. Open and closed PRs fail closed."""
    url = _pull_request_url(url)
    if not shutil.which("gh"):
        raise PullRequestError("GitHub CLI (gh) is not installed")
    raw = _run(["gh", "pr", "view", url, "--json", "state,mergedAt"], Path.cwd())
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PullRequestError("GitHub did not return pull request JSON") from exc
    if not isinstance(payload, dict):
        raise PullRequestError("GitHub did not return pull request JSON")
    state = payload.get("state")
    merged_at = payload.get("mergedAt")
    if state != "MERGED" or not isinstance(merged_at, str) or not merged_at.strip():
        raise PullRequestError(f"Pull request is not merged (state={state})")


def github_user_login() -> str:
    """Login returned by the authenticated gh CLI. Missing or malformed logins fail closed."""
    if not shutil.which("gh"):
        raise PullRequestError("GitHub CLI (gh) is not installed")
    login = _run(["gh", "api", "user", "--jq", ".login"], Path.cwd()).strip()
    if not _GITHUB_LOGIN.fullmatch(login):
        raise PullRequestError("Authenticated GitHub login is missing or not a single login")
    return login


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
