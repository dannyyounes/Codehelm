import subprocess
from unittest.mock import patch
import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.integrations.github_pr import publish_pr, PullRequestError
from codehelm.orchestrator.lifecycle import LifecycleEngine
from codehelm.quality.contract import ContractDecision, load_policy


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
    assert all("merge" not in part for c in commands for part in c)


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
    assert "draft PR" in result.output


@pytest.mark.parametrize("policy,flags,kind", [
    ("schema_version: 1\npr_ready: true\n", (), "ready PR"),
    ("schema_version: 1\npr_ready: true\n", ("--draft",), "draft PR"),
    ("schema_version: 1\npr_ready: false\n", (), "draft PR"),
    (None, ("--ready",), "ready PR"),
])
def test_pr_create_dry_run_respects_pr_ready(tmp_path, monkeypatch, policy, flags, kind):
    monkeypatch.chdir(tmp_path)
    if policy is not None:
        state = tmp_path / ".codehelm"
        state.mkdir()
        (state / "quality.yaml").write_text(policy, encoding="utf-8")
    result = CliRunner().invoke(
        app, ["pr-create", "DEV-1", "--title", "T", "--body", "B", *flags])
    assert result.exit_code == 0, result.output
    assert f"create a {kind}." in result.output
    assert "merge" not in result.output.lower()


def test_pr_ready_omits_draft_and_does_not_merge(tmp_path):
    responses = iter(["", "codehelm/DEV-1", "abc123", "", "", "https://github.com/o/r/pull/8"])
    with patch("codehelm.integrations.github_pr.shutil.which", return_value="/usr/bin/gh"), \
         patch("codehelm.integrations.github_pr._run", side_effect=lambda *a: next(responses)) as run:
        result = publish_pr(tmp_path, "codehelm/DEV-1", "Title", "Body", draft=False)
    assert result.url.endswith("/pull/8")
    commands = [call.args[0] for call in run.call_args_list]
    create = next(c for c in commands if c[:3] == ["gh", "pr", "create"])
    assert "--draft" not in create
    assert all("merge" not in part for command in commands for part in command)


def test_pr_ready_policy_defaults(tmp_path):
    missing = tmp_path / "missing.yaml"
    assert load_policy(missing).pr_ready is False
    path = tmp_path / "quality.yaml"
    path.write_text("schema_version: 1\n", encoding="utf-8")
    assert load_policy(path).pr_ready is False
    path.write_text("schema_version: 1\npr_ready: false\n", encoding="utf-8")
    assert load_policy(path).pr_ready is False
    path.write_text("schema_version: 1\npr_ready:\n", encoding="utf-8")
    assert load_policy(path).pr_ready is False
    path.write_text("schema_version: 1\npr_ready: true\n", encoding="utf-8")
    assert load_policy(path).pr_ready is True
    path.write_text("schema_version: 1\npr_ready: 'true'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="pr_ready"):
        load_policy(path)


def _invoke_pr(tmp_path, policy, *flags):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init",), ("config", "user.email", "t@example.org"), ("config", "user.name", "T")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    state = tmp_path / ".codehelm"
    state.mkdir()
    if policy is not None:
        (state / "quality.yaml").write_text(policy, encoding="utf-8")
    LifecycleEngine(repo, state / "state.db").store.transition(
        "DEV-1", "ready_for_pr" if policy is not None else "awaiting_pr",
        provider="claude", workspace=str(repo))
    with patch("codehelm.orchestrator.lifecycle.assess",
               return_value=ContractDecision("ready_for_pr", (), False)), \
         patch("codehelm.orchestrator.lifecycle.publish_pr",
               return_value=type("PR", (), {"url": "https://github.com/o/r/pull/7"})()) as publish:
        result = CliRunner().invoke(
            app, ["pr-create", "DEV-1", "--title", "T", "--body", "B", "--repo", str(repo),
                  "--execute", *flags])
    assert result.exit_code == 0, result.output
    assert "merge" not in result.output.lower()
    return publish.call_args.kwargs["draft"], result.output


@pytest.mark.parametrize("policy,flags,draft", [
    (None, (), True),
    ("schema_version: 1\n", (), True),
    ("schema_version: 1\npr_ready: false\n", (), True),
    ("schema_version: 1\npr_ready: true\n", (), False),
    ("schema_version: 1\npr_ready: true\n", ("--draft",), True),
    ("schema_version: 1\npr_ready: false\n", ("--ready",), False),
    (None, ("--ready",), False),
    ("schema_version: 1\npr_ready: true\n", ("--ready", "--draft"), True),
])
def test_pr_create_draft_wiring(tmp_path, monkeypatch, policy, flags, draft):
    monkeypatch.chdir(tmp_path)
    chosen, output = _invoke_pr(tmp_path, policy, *flags)
    assert chosen is draft
    if draft:
        assert "Draft pull request created:" in output
    else:
        assert "Draft pull request" not in output
        assert "Pull request created:" in output
