import sqlite3
import subprocess
from unittest.mock import patch
import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.orchestrator.lifecycle import LifecycleEngine, TaskStore
from codehelm.providers.adapters import AgentResult


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.org")
    git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    return repo


def test_success_persists_awaiting_validation(repo, tmp_path):
    db = tmp_path / "state.db"
    engine = LifecycleEngine(repo, db)
    with patch("codehelm.orchestrator.lifecycle.CLIProvider.execute",
               return_value=AgentResult("claude", 0, "ok", "")):
        result = engine.execute("DEV-123", "Fix bug", "claude")
    assert result.exit_code == 0
    task = TaskStore(db).get("DEV-123")
    assert task.status == "awaiting_validation"
    assert task.workspace and task.provider == "claude"
    with sqlite3.connect(db) as conn:
        statuses = [r[0] for r in conn.execute(
            "SELECT status FROM task_events WHERE task_id=? ORDER BY id", ("DEV-123",))]
    assert statuses == ["queued", "running", "awaiting_validation"]


def test_failure_is_persisted(repo, tmp_path):
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    with patch("codehelm.orchestrator.lifecycle.CLIProvider.execute",
               return_value=AgentResult("codex", 3, "", "failed")):
        engine.execute("DEV-124", "Fix bug", "codex")
    assert engine.store.get("DEV-124").status == "failed"


def test_exception_is_persisted(repo, tmp_path):
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    with patch("codehelm.orchestrator.lifecycle.CLIProvider.execute",
               side_effect=RuntimeError("unexpected")):
        with pytest.raises(RuntimeError):
            engine.execute("DEV-125", "Fix bug", "claude")
    assert engine.store.get("DEV-125").status == "failed"


def test_duplicate_task_refused(repo, tmp_path):
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    with patch("codehelm.orchestrator.lifecycle.CLIProvider.execute",
               return_value=AgentResult("claude", 0, "", "")):
        engine.execute("DEV-126", "Fix", "claude")
    with pytest.raises(ValueError, match="already exists"):
        engine.execute("DEV-126", "Fix", "claude")


def test_run_dry_run_does_not_launch(repo, monkeypatch):
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(app, ["run", "--jira", "DEV-1", "--prompt", "Fix"])
    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert not (repo / ".codehelm").exists()
