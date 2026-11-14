"""Documentation agent: update docs based on implementation, then verify changes."""
from dataclasses import dataclass
from pathlib import Path
import subprocess
import hashlib

from codehelm.providers.adapters import CLIProvider


@dataclass(frozen=True)
class DocumentationResult:
    passed: bool
    detail: str


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


def run_documentation_agent(workspace: Path, provider: str,
                            timeout: int = 600) -> DocumentationResult:
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
    )
    result = CLIProvider(provider).execute(prompt, workspace, timeout)
    if result.exit_code:
        return DocumentationResult(False, f"Documentation agent failed: {result.error[-1500:]}")
    after = _changed_paths(workspace)
    after_hashes = fingerprints(after)
    documentation_paths = lambda paths: {
        path for path in paths
        if path == "README.md" or path.startswith("docs/") or
           path.endswith((".md", ".rst")) or path.startswith("CHANGELOG")
    }
    changed = {path for path in after if before_hashes.get(path) != after_hashes.get(path)}
    if not changed.issubset(documentation_paths(changed)):
        return DocumentationResult(False, "Documentation agent modified non-documentation files")
    if not documentation_paths(changed):
        return DocumentationResult(False, "No documentation changes detected")
    return DocumentationResult(True, result.output[-2000:] or "Documentation changes detected")
