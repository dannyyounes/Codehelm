"""High-risk approval records the authenticated GitHub login."""
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.integrations.github_pr import PullRequestError, github_user_login
from codehelm.orchestrator.lifecycle import TaskStore


def test_github_login_uses_gh_and_rejects_a_bad_value(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("codehelm.integrations.github_pr.shutil.which", return_value=None):
        with pytest.raises(PullRequestError, match="not installed"):
            github_user_login()
    with patch("codehelm.integrations.github_pr.shutil.which", return_value="/usr/bin/gh"), \
         patch("codehelm.integrations.github_pr._run", return_value="not a login") as run:
        with pytest.raises(PullRequestError, match="not a single login"):
            github_user_login()
    assert run.call_args.args[0] == ["gh", "api", "user", "--jq", ".login"]
    with patch("codehelm.integrations.github_pr.shutil.which", return_value="/usr/bin/gh"), \
         patch("codehelm.integrations.github_pr._run", return_value="danny-younes\n"):
        assert github_user_login() == "danny-younes"
    with patch("codehelm.integrations.github_pr.shutil.which", return_value="/usr/bin/gh"), \
         patch("codehelm.integrations.github_pr._run", side_effect=PullRequestError("auth failed")):
        with pytest.raises(PullRequestError, match="auth failed"):
            github_user_login()


def test_approve_records_the_matching_login(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".codehelm").mkdir()
    store = TaskStore(tmp_path / ".codehelm" / "state.db")
    store.initialize()
    with patch("codehelm.cli.main.github_user_login", return_value="dannyyounes"):
        result = CliRunner().invoke(
            app, ["quality-approve", "DEV-1", "--approver", " dannyyounes ", "--execute"])
    assert result.exit_code == 0, result.output
    rows = store.evidence_for("DEV-1", 1)
    assert [(row.gate, row.source, row.identity, row.passed) for row in rows] == [
        ("human_approval", "human", "dannyyounes", True)]


def test_approve_refuses_a_different_name_and_a_github_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".codehelm").mkdir()
    store = TaskStore(tmp_path / ".codehelm" / "state.db")
    store.initialize()
    with patch("codehelm.cli.main.github_user_login", return_value="dannyyounes"):
        mismatch = CliRunner().invoke(
            app, ["quality-approve", "DEV-1", "--approver", "someone-else", "--execute"])
    assert mismatch.exit_code == 1
    assert "does not match" in mismatch.output
    with patch("codehelm.cli.main.github_user_login", side_effect=PullRequestError("not logged in")):
        failed = CliRunner().invoke(
            app, ["quality-approve", "DEV-1", "--approver", "dannyyounes", "--execute"])
    assert failed.exit_code == 1
    assert "not logged in" in failed.output
    assert store.evidence_for("DEV-1", 1) == []


def test_approve_dry_run_does_not_call_github(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("codehelm.cli.main.github_user_login") as lookup:
        result = CliRunner().invoke(app, ["quality-approve", "DEV-1", "--approver", "danny"])
    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert not lookup.called
