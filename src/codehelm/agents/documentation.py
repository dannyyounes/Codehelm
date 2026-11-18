"""Documentation agent: update docs based on implementation, then verify changes."""
from dataclasses import dataclass
from pathlib import Path
import subprocess
import hashlib

from codehelm.providers.adapters import CLIProvider
from codehelm.quality.contract import collect_diff, load_policy


@dataclass(frozen=True)
class DocumentationResult:
    passed: bool
    detail: str


def _is_documentation_path(path: str) -> bool:
    return (
        path == "README.md" or path.startswith("docs/")
        or path.endswith((".md", ".rst")) or path.startswith("CHANGELOG")
    )


def _changed_paths(workspace: Path) -> set[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        cwd=workspace, text=True, capture_output=True, check=True,
    )
    paths = set()
    for line in result.stdout.splitlines():
        # Git porcelain v1: two status characters, space, then path.
        value = line[3:]
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        paths.add(value.strip('"'))
    return paths


def _story_clause(title: str, description: str,
                  criteria: list[str] | tuple[str, ...] | None) -> str:
    title = (title or "").strip()
    description = (description or "").strip()
    items = [item.strip() for item in (criteria or []) if item and item.strip()]
    if not title and not description and not items:
        return ""
    listed = "\n".join(f"- {item}" for item in items)
    return (
        f"\n\nStory title: {title}\nStory description: {description}\n"
        f"Acceptance criteria:\n{listed}\n"
        "Respect any documentation scope or line limits stated there "
        "and make no edit if documentation is already complete."
    )


def _already_included(workspace: Path, policy_path: Path | None) -> bool:
    base_branch = load_policy(policy_path).base_branch if policy_path is not None else ""
    files, lines = collect_diff(workspace, base_branch)
    if lines < 0:
        raise RuntimeError("Could not compute the documentation diff against the resolved base")
    return any(_is_documentation_path(path) for path in files)


def _agent_changed(before: set[str], after: set[str],
                   before_hashes: dict[str, str], after_hashes: dict[str, str]) -> set[str]:
    """Paths whose snapshot differs, including a dirty path that is later clean or missing."""
    return {
        path for path in before | after
        if (path in before) != (path in after)
        or before_hashes.get(path) != after_hashes.get(path)
    }


def run_documentation_agent(workspace: Path, provider: str,
                            timeout: int = 600, *,
                            story_title: str = "",
                            story_description: str = "",
                            acceptance_criteria: list[str] | tuple[str, ...] | None = None,
                            policy_path: Path | None = None) -> DocumentationResult:
    """Require a documentation-file change; do not trust model self-attestation."""
    before = _changed_paths(workspace)
    def fingerprints(paths: set[str]) -> dict[str, str]:
        return {
            path: hashlib.sha256((workspace / path).read_bytes()).hexdigest()
            for path in paths if (workspace / path).is_file()
        }
    before_hashes = fingerprints(before)
    prompt = (
        "You are the documentation specialist for this software change. "
        "Inspect the actual implementation and tests, then update relevant "
        "README, docs/, API reference, user or developer guides as appropriate. "
        "Keep changes factual, concise, and consistent with existing behavior. "
        "Do not claim unimplemented functionality exists. Do not edit source code "
        "or tests. Document how to use and verify the change. "
        "If documentation cannot be completed, explain the blocker."
    ) + _story_clause(story_title, story_description, acceptance_criteria)
    result = CLIProvider(provider, role="docs").execute(prompt, workspace, timeout)
    if result.exit_code:
        return DocumentationResult(False, f"Documentation agent failed: {result.error[-1500:]}")
    after = _changed_paths(workspace)
    # Hash the union so a path that left git status (restored or removed) is still compared.
    union = before | after
    after_hashes = fingerprints(union)
    changed = _agent_changed(before, after, before_hashes, after_hashes)
    docs = {path for path in changed if _is_documentation_path(path)}
    if not changed.issubset(docs):
        return DocumentationResult(False, "Documentation agent modified non-documentation files")
    if not docs:
        if _already_included(workspace, policy_path):
            return DocumentationResult(True, "Documentation already included in the change")
        return DocumentationResult(False, "No documentation changes detected")
    return DocumentationResult(True, result.output[-2000:] or "Documentation changes detected")
