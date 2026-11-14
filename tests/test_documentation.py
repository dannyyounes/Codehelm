from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest
from codehelm.agents.documentation import run_documentation_agent
from codehelm.orchestrator.lifecycle import LifecycleEngine
from codehelm.providers.adapters import AgentResult


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def workspace(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.org")
    git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("Original")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    return repo


def test_documentation_requires_actual_change(workspace):
    with patch("codehelm.agents.documentation.CLIProvider.execute",
               return_value=AgentResult("claude", 0, "done", "")):
        assert not run_documentation_agent(workspace, "claude").passed


def test_documentation_change_accepted(workspace):
    def edit(*args):
        (workspace / "README.md").write_text("Updated")
        return AgentResult("claude", 0, "done", "")
    with patch("codehelm.agents.documentation.CLIProvider.execute", side_effect=edit):
        assert run_documentation_agent(workspace, "claude").passed


def test_code_change_rejected(workspace):
    def edit(*args):
        (workspace / "main.py").write_text("print(1)")
        return AgentResult("claude", 0, "done", "")
    with patch("codehelm.agents.documentation.CLIProvider.execute", side_effect=edit):
        assert not run_documentation_agent(workspace, "claude").passed


def test_documentation_lifecycle_transition(workspace, tmp_path):
    engine = LifecycleEngine(workspace, tmp_path / "state.db")
    engine.store.transition("DEV-10", "awaiting_documentation", provider="codex",
                            workspace=str(workspace))
    def edit(*args):
        (workspace / "README.md").write_text("Updated")
        return AgentResult("claude", 0, "done", "")
    with patch("codehelm.agents.documentation.CLIProvider.execute", side_effect=edit):
        assert engine.document("DEV-10", "claude")
    assert engine.store.get("DEV-10").status == "awaiting_post_docs_validation"


def test_cannot_skip_review(workspace, tmp_path):
    engine = LifecycleEngine(workspace, tmp_path / "state.db")
    engine.store.transition("DEV-11", "awaiting_validation", workspace=str(workspace))
    with pytest.raises(ValueError, match="awaiting_documentation"):
        engine.document("DEV-11", "claude")
