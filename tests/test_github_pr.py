from unittest.mock import patch
import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.integrations.github_pr import publish_pr, PullRequestError
from codehelm.orchestrator.lifecycle import LifecycleEngine


def test_pr_rejects_dirty_tree(tmp_path):
    with patch("codehelm.integrations.github_pr.shutil.which", return_value="/usr/bin/gh"), \
         patch("codehelm.integrations.github_pr._run", return_value=" M README.md"):
        with pytest.raises(PullRequestError, match="uncommitted"):
            publish_pr(tmp_path, "codehelm/DEV-1", "Title", "Body")


def test_pr_push_and_create_draft(tmp_path):
    responses = iter(["", "codehelm/DEV-1", "abc123", "", "", "https://github.com/o/r/pull/7"])
    with patch("codehelm.integrations.github_pr.shutil.which", return_value="/usr/bin/gh"), \
         patch("codehelm.integrations.github_pr._run", side_effect=lambda *a: next(responses)) as run:
        result = publish_pr(tmp_path, "codehelm/DEV-1", "Title", "Body")
    assert result.url.endswith("/pull/7")
    commands = [call.args[0] for call in run.call_args_list]
    assert ["git", "push", "-u", "origin", "codehelm/DEV-1"] in commands
    assert any(c[:3] == ["gh", "pr", "create"] and "--draft" in c for c in commands)


def test_pr_lifecycle_gate(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (("init",), ("config", "user.email", "test@example.org"),
                ("config", "user.name", "Test")):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, capture_output=True)
    (repo / "README.md").write_text("hello")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    engine.store.transition("DEV-1", "awaiting_validation", workspace=str(repo))
    with pytest.raises(ValueError, match="awaiting_pr"):
        engine.create_pull_request("DEV-1", "Title", "Body")


def test_pr_cli_dry_run():
    result = CliRunner().invoke(app, ["pr-create", "DEV-1", "--title", "Title", "--body", "Body"])
    assert result.exit_code == 0
    assert "Dry run" in result.output
