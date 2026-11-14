from unittest.mock import patch
from codehelm.quality.gates import GateResult, command_gate, independent_review
from codehelm.orchestrator.lifecycle import LifecycleEngine
from codehelm.providers.adapters import AgentResult
import subprocess


def test_empty_command_fails_closed(tmp_path):
    assert not command_gate(tmp_path, []).passed


def test_failed_command_blocks(tmp_path):
    assert not command_gate(tmp_path, ["python", "-c", "raise SystemExit(2)"]).passed


def test_reviewer_must_be_independent(tmp_path):
    assert not independent_review(tmp_path, "claude", "claude").passed


def test_reviewer_cannot_change_workspace(tmp_path):
    with patch("codehelm.quality.gates.git_snapshot", side_effect=["original", "modified"]), \
         patch("codehelm.quality.gates.CLIProvider.execute",
               return_value=AgentResult("codex", 0, "APPROVED", "")):
        assert not independent_review(tmp_path, "claude", "codex").passed


def test_review_requires_exact_verdict(tmp_path):
    with patch("codehelm.quality.gates.git_snapshot", side_effect=["original", "original"]), \
         patch("codehelm.quality.gates.CLIProvider.execute",
               return_value=AgentResult("codex", 0, "Looks good", "")):
        assert not independent_review(tmp_path, "claude", "codex").passed


def test_pipeline_passes_to_documentation_gate(tmp_path):
    from codehelm.orchestrator.lifecycle import TaskStore
    from codehelm.orchestrator.lifecycle import LifecycleEngine
    from unittest.mock import patch
    from codehelm.quality.gates import GateResult
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init"], ["config", "user.email", "test@example.org"],
                 ["config", "user.name", "Test"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "README.md").write_text("initial")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True)
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    workspace = engine.worktrees.create("DEV-9")
    engine.store.transition("DEV-9", "awaiting_validation", provider="claude", workspace=str(workspace.path))
    with patch("codehelm.orchestrator.lifecycle.independent_review",
               return_value=GateResult(True, "APPROVED")):
        assert engine.validate("DEV-9", ["python", "-c", "pass"],
                               ["python", "-c", "pass"], "codex")
    assert engine.store.get("DEV-9").status == "awaiting_documentation"
