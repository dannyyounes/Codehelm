"""Opt-in merge stays off unless gates, required approval, and green CI all hold."""
import json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.integrations.github_pr import (
    PullRequestError, github_merge_pull_request, github_pull_request_green,
    pull_request_head_if_green,
)
from codehelm.orchestrator.lifecycle import TaskStore
from codehelm.quality.contract import (
    ContractDecision, QualityPolicy, auto_merge_block, load_policy,
)

URL = "https://github.com/acme/widgets/pull/9"
HEAD = "a" * 40
runner = CliRunner()


def test_auto_merge_block_requires_the_flag_gates_and_human_approval():
    ready = ContractDecision("ready_for_pr", (), True)
    assert auto_merge_block(QualityPolicy(auto_merge=False), ready) == "auto-merge is off"
    assert auto_merge_block(QualityPolicy(present=False, auto_merge=True), ready) == "auto-merge is off"
    blocked = ContractDecision("blocked", ("tests_not_verified",), True)
    assert auto_merge_block(QualityPolicy(auto_merge=True), blocked) == "quality gates have not all passed"
    waiting = ContractDecision("awaiting_human_approval", (), True)
    assert auto_merge_block(QualityPolicy(auto_merge=True), waiting) == "required human approval is absent"
    unrequired = ContractDecision("ready_for_pr", (), False)
    assert auto_merge_block(QualityPolicy(auto_merge=True), unrequired) == "required human approval is absent"
    assert auto_merge_block(QualityPolicy(auto_merge=True), ready) is None


def test_policy_auto_merge_defaults_off_and_rejects_a_string(tmp_path):
    path = tmp_path / "quality.yaml"
    path.write_text("schema_version: 1\n", encoding="utf-8")
    assert load_policy(path).auto_merge is False
    path.write_text("schema_version: 1\nauto_merge: off\n", encoding="utf-8")
    assert load_policy(path).auto_merge is False
    path.write_text("schema_version: 1\nauto_merge: true\n", encoding="utf-8")
    assert load_policy(path).auto_merge is True
    path.write_text("schema_version: 1\nauto_merge: 'true'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="auto_merge"):
        load_policy(path)


def test_green_checks_return_the_head_and_everything_else_is_refused():
    payload = {
        "state": "OPEN",
        "headRefOid": HEAD,
        "statusCheckRollup": [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "StatusContext", "state": "SUCCESS"},
        ],
    }
    assert pull_request_head_if_green(payload) == HEAD
    for broken in (
        {**payload, "state": "MERGED"},
        {**payload, "headRefOid": "abc"},
        {**payload, "statusCheckRollup": []},
        {**payload, "statusCheckRollup": [
            {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None}]},
        {**payload, "statusCheckRollup": [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SKIPPED"}]},
        {**payload, "statusCheckRollup": [
            {"__typename": "StatusContext", "state": "PENDING"}]},
        ["OPEN"],
    ):
        with pytest.raises(PullRequestError):
            pull_request_head_if_green(broken)


def test_merge_command_matches_the_head_and_requires_a_merged_result():
    merged = json.dumps({"state": "MERGED", "mergedAt": "2026-11-17T00:00:00Z"})
    with patch("codehelm.integrations.github_pr.shutil.which", return_value="/usr/bin/gh"), \
         patch("codehelm.integrations.github_pr._run", side_effect=["merged", merged]) as run:
        github_merge_pull_request(URL, HEAD)
    assert run.call_args_list[0].args[0] == [
        "gh", "pr", "merge", URL, "--merge", "--match-head-commit", HEAD]
    assert "--auto" not in run.call_args_list[0].args[0]
    assert "--admin" not in run.call_args_list[0].args[0]
    open_pr = json.dumps({"state": "OPEN", "mergedAt": None})
    with patch("codehelm.integrations.github_pr.shutil.which", return_value="/usr/bin/gh"), \
         patch("codehelm.integrations.github_pr._run", side_effect=["queued", open_pr]):
        with pytest.raises(PullRequestError, match="was not merged"):
            github_merge_pull_request(URL, HEAD)


def test_green_view_uses_gh_and_rejects_a_bad_url():
    raw = json.dumps({
        "state": "OPEN", "headRefOid": HEAD,
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
    })
    with patch("codehelm.integrations.github_pr.shutil.which", return_value="/usr/bin/gh"), \
         patch("codehelm.integrations.github_pr._run", return_value=raw) as run:
        assert github_pull_request_green(URL) == HEAD
    assert run.call_args.args[0][:3] == ["gh", "pr", "view"]
    with pytest.raises(PullRequestError, match="not a GitHub pull request"):
        github_pull_request_green("https://example.com/pull/1")


def _policy(tmp_path, enabled: bool):
    (tmp_path / ".codehelm").mkdir(exist_ok=True)
    value = "true" if enabled else "false"
    (tmp_path / ".codehelm" / "quality.yaml").write_text(
        f"schema_version: 1\nauto_merge: {value}\n", encoding="utf-8")


def _task(tmp_path):
    store = TaskStore(tmp_path / ".codehelm" / "state.db")
    store.initialize()
    store.transition("DEV-1", "pr_created", error=URL, workspace=str(tmp_path))
    return store


def test_dry_run_does_not_call_github(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _policy(tmp_path, False)
    with patch("codehelm.cli.main.github_pull_request_green") as checks, \
         patch("codehelm.cli.main.github_merge_pull_request") as merge:
        off = runner.invoke(app, ["merge-ready", "DEV-1"])
    assert off.exit_code == 0, off.output
    assert "Auto-merge is off" in off.output
    assert not checks.called and not merge.called
    _policy(tmp_path, True)
    _task(tmp_path)
    ready = ContractDecision("ready_for_pr", (), True)
    with patch("codehelm.cli.main.assess", return_value=ready), \
         patch("codehelm.cli.main.github_pull_request_green") as checks, \
         patch("codehelm.cli.main.github_merge_pull_request") as merge:
        allowed = runner.invoke(app, ["merge-ready", "DEV-1"])
    assert allowed.exit_code == 0, allowed.output
    assert "Not merged" in allowed.output
    assert not checks.called and not merge.called


def test_execute_merges_only_when_every_condition_holds(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _policy(tmp_path, True)
    store = _task(tmp_path)
    ready = ContractDecision("ready_for_pr", (), True)
    with patch("codehelm.cli.main.assess", return_value=ready), \
         patch("codehelm.cli.main.github_pull_request_green",
               side_effect=PullRequestError("CI is not green")), \
         patch("codehelm.cli.main.github_merge_pull_request") as merge:
        red = runner.invoke(app, ["merge-ready", "DEV-1", "--execute"])
    assert red.exit_code == 1
    assert "not green" in red.output
    assert not merge.called
    assert store.get("DEV-1").status == "pr_created"
    with patch("codehelm.cli.main.assess", return_value=ContractDecision("ready_for_pr", (), False)), \
         patch("codehelm.cli.main.github_pull_request_green") as checks, \
         patch("codehelm.cli.main.github_merge_pull_request") as merge:
        unrequired = runner.invoke(app, ["merge-ready", "DEV-1", "--execute"])
    assert unrequired.exit_code == 1
    assert "human approval" in unrequired.output
    assert not checks.called and not merge.called
    with patch("codehelm.cli.main.assess", return_value=ready), \
         patch("codehelm.cli.main.github_pull_request_green", return_value=HEAD) as checks, \
         patch("codehelm.cli.main.github_merge_pull_request") as merge:
        merged = runner.invoke(app, ["merge-ready", "DEV-1", "--execute"])
    assert merged.exit_code == 0, merged.output
    assert "not completed" in merged.output
    checks.assert_called_once_with(URL)
    merge.assert_called_once_with(URL, HEAD)
    assert store.get("DEV-1").status == "pr_created"


def test_execute_while_off_does_not_call_github(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _policy(tmp_path, False)
    _task(tmp_path)
    with patch("codehelm.cli.main.github_merge_pull_request") as merge:
        result = runner.invoke(app, ["merge-ready", "DEV-1", "--execute"])
    assert result.exit_code == 1
    assert "off" in result.output
    assert not merge.called
