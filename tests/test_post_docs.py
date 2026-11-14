import subprocess
from unittest.mock import patch
import pytest

from codehelm.orchestrator.lifecycle import LifecycleEngine
from codehelm.quality.gates import GateResult


@pytest.fixture
def engine(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    git("init")
    git("config", "user.email", "test@example.org")
    git("config", "user.name", "Test")
    (repo / "README.md").write_text("Initial")
    git("add", ".")
    git("commit", "-m", "initial")
    result = LifecycleEngine(repo, tmp_path / "state.db")
    result.store.transition("DEV-22", "awaiting_post_docs_validation",
                            provider="claude", workspace=str(repo))
    return result


def test_post_docs_passes_to_pr_gate(engine):
    with patch("codehelm.orchestrator.lifecycle.independent_review",
               return_value=GateResult(True, "APPROVED")):
        assert engine.validate_after_docs("DEV-22", ["python", "-c", "pass"],
                                          ["python", "-c", "pass"], "codex")
    assert engine.store.get("DEV-22").status == "awaiting_pr"


def test_post_docs_test_failure_blocks(engine):
    assert not engine.validate_after_docs("DEV-22", ["python", "-c", "raise SystemExit(1)"],
                                          ["python", "-c", "pass"], "codex")
    assert engine.store.get("DEV-22").status == "blocked"


def test_post_docs_review_rejection(engine):
    with patch("codehelm.orchestrator.lifecycle.independent_review",
               return_value=GateResult(False, "CHANGES_REQUESTED")):
        assert not engine.validate_after_docs("DEV-22", ["python", "-c", "pass"],
                                              ["python", "-c", "pass"], "codex")
    assert engine.store.get("DEV-22").status == "changes_requested"


def test_post_docs_cannot_skip(engine):
    engine.store.transition("DEV-23", "awaiting_validation", provider="claude",
                            workspace=str(engine.worktrees.repository))
    with pytest.raises(ValueError, match="awaiting_post_docs_validation"):
        engine.validate_after_docs("DEV-23", ["python"], ["python"], "codex")
