"""Story completion requires gh to report the recorded pull request merged."""
import json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.integrations.github_pr import PullRequestError, github_pull_request_merged
from codehelm.orchestrator.durable import DispatchBudget, DurableScheduler
from codehelm.planning.planner import parse_plan


PLAN = {
    "epic_title": "Feature", "epic_description": "Build it",
    "stories": [
        {"id": "S1", "title": "First", "description": "First",
         "acceptance_criteria": ["Works"], "dependencies": []},
        {"id": "S2", "title": "Second", "description": "Second",
         "acceptance_criteria": ["Works"], "dependencies": ["S1"]},
    ],
}
URL = "https://github.com/acme/widgets/pull/9"


def scheduler_for(tmp_path):
    return DurableScheduler(tmp_path / "state.db", parse_plan(json.dumps(PLAN)), "DEV")


def ready(scheduler):
    scheduler.claim(DispatchBudget())
    scheduler.finish("S1", "awaiting_validation")
    scheduler.store.transition("DEV-S1-A1", "pr_created", error=URL)


def test_merged_payload_is_accepted_and_other_states_are_refused():
    merged = json.dumps({"state": "MERGED", "mergedAt": "2026-11-16T06:00:00Z"})
    with patch("codehelm.integrations.github_pr.shutil.which", return_value="/usr/bin/gh"), \
         patch("codehelm.integrations.github_pr._run", return_value=merged) as run:
        github_pull_request_merged(URL)
    assert run.call_args.args[0] == ["gh", "pr", "view", URL, "--json", "state,mergedAt"]
    for payload in (
        {"state": "OPEN", "mergedAt": None},
        {"state": "CLOSED", "mergedAt": None},
        {"state": "MERGED", "mergedAt": ""},
        {"state": "merged", "mergedAt": "2026-11-16T06:00:00Z"},
        ["MERGED"],
    ):
        with patch("codehelm.integrations.github_pr.shutil.which", return_value="/usr/bin/gh"), \
             patch("codehelm.integrations.github_pr._run", return_value=json.dumps(payload)):
            with pytest.raises(PullRequestError, match="not merged|not return"):
                github_pull_request_merged(URL)


def test_bad_url_and_missing_gh_are_refused():
    with pytest.raises(PullRequestError, match="not a GitHub pull request"):
        github_pull_request_merged("https://example.com/acme/widgets/pull/9")
    with patch("codehelm.integrations.github_pr.shutil.which", return_value=None):
        with pytest.raises(PullRequestError, match="not installed"):
            github_pull_request_merged(URL)
    with patch("codehelm.integrations.github_pr.shutil.which", return_value="/usr/bin/gh"), \
         patch("codehelm.integrations.github_pr._run", return_value="not-json"):
        with pytest.raises(PullRequestError, match="not return"):
            github_pull_request_merged(URL)


def test_unmerged_pull_request_does_not_complete_the_story(tmp_path):
    scheduler = scheduler_for(tmp_path)
    ready(scheduler)
    with patch("codehelm.orchestrator.durable.github_pull_request_merged",
               side_effect=PullRequestError("Pull request is not merged (state=OPEN)")):
        with pytest.raises(ValueError, match="not merged"):
            scheduler.complete("S1", human_verified_merge=True)
    assert scheduler.snapshot()["S1"] == ("awaiting_validation", 1)
    assert scheduler.claim(DispatchBudget()) == []


def test_merged_pull_request_unlocks_the_dependent(tmp_path):
    scheduler = scheduler_for(tmp_path)
    ready(scheduler)
    with patch("codehelm.orchestrator.durable.github_pull_request_merged") as check:
        scheduler.complete("S1", human_verified_merge=True)
    check.assert_called_once_with(URL)
    assert scheduler.snapshot()["S1"][0] == "completed"
    assert scheduler.claim(DispatchBudget()) == ["S2"]


def test_schedule_complete_dry_run_does_not_call_github(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(PLAN), encoding="utf-8")
    with patch("codehelm.orchestrator.durable.github_pull_request_merged") as check:
        result = CliRunner().invoke(app, [
            "schedule-complete", "S1", "--plan-file", str(plan), "--prefix", "DEV",
            "--confirmed-merged"])
    assert result.exit_code == 0
    assert "No change" in result.output
    assert not check.called


def test_schedule_complete_command_uses_the_github_check(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(PLAN), encoding="utf-8")
    (tmp_path / ".codehelm").mkdir()
    scheduler = DurableScheduler(tmp_path / ".codehelm" / "state.db", parse_plan(json.dumps(PLAN)), "DEV")
    ready(scheduler)
    with patch("codehelm.orchestrator.durable.github_pull_request_merged",
               side_effect=PullRequestError("Pull request is not merged (state=OPEN)")):
        refused = CliRunner().invoke(app, [
            "schedule-complete", "S1", "--plan-file", str(plan), "--prefix", "DEV",
            "--confirmed-merged", "--execute"])
    assert refused.exit_code == 1
    assert "not merged" in refused.output
    assert scheduler.snapshot()["S1"][0] == "awaiting_validation"
    with patch("codehelm.orchestrator.durable.github_pull_request_merged") as check:
        allowed = CliRunner().invoke(app, [
            "schedule-complete", "S1", "--plan-file", str(plan), "--prefix", "DEV",
            "--confirmed-merged", "--execute"])
    assert allowed.exit_code == 0, allowed.output
    check.assert_called_once_with(URL)
    assert scheduler.snapshot()["S1"][0] == "completed"
