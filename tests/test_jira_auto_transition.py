"""Scheduler Jira transitions are opt-in, dry-run, and recorded."""
import json
import sqlite3
from unittest.mock import patch

import pytest

from codehelm.integrations.jira import JiraError
from codehelm.orchestrator.durable import DispatchBudget, DurableScheduler, JiraTransitionError
from codehelm.planning.planner import PlanningError, parse_plan
from codehelm.quality.contract import load_policy


def _plan(key: str | None = "DEV-9") -> str:
    story = {
        "id": "S1", "title": "First", "description": "First",
        "acceptance_criteria": ["Works"], "dependencies": [],
    }
    if key is not None:
        story["jira_key"] = key
    return json.dumps({
        "epic_title": "Feature", "epic_description": "Build it", "stories": [story],
    })


def _policy(tmp_path, body: str) -> None:
    (tmp_path / "quality.yaml").write_text("schema_version: 1\n" + body, encoding="utf-8")


def _events(database):
    with sqlite3.connect(database) as db:
        return db.execute(
            "SELECT status, detail FROM task_events ORDER BY id").fetchall()


def test_policy_defaults_off_and_refuses_invented_ids(tmp_path):
    _policy(tmp_path, "")
    policy = load_policy(tmp_path / "quality.yaml")
    assert policy.jira_transitions == () and policy.jira_transition_execute is False
    _policy(tmp_path, "jira_transitions: false\n")
    assert load_policy(tmp_path / "quality.yaml").jira_transitions == ()
    _policy(tmp_path, "jira_transitions: off\n")
    assert load_policy(tmp_path / "quality.yaml").jira_transitions == ()
    for body in (
        "jira_transitions: true\n",
        "jira_transitions: {}\n",
        "jira_transitions:\n  claimed: '31'\n",
        "jira_transitions:\n  failed: 31\n",
        "jira_transition_execute: true\n",
    ):
        _policy(tmp_path, body)
        with pytest.raises(ValueError):
            load_policy(tmp_path / "quality.yaml")


def test_bad_jira_key_is_rejected():
    with pytest.raises(PlanningError, match="jira_key"):
        parse_plan(_plan("dev-9"))
    plan = parse_plan(_plan(None))
    assert plan.stories[0].jira_key == ""
    assert "jira_key" not in plan.to_dict()["stories"][0]


def test_dry_run_records_the_transition_and_does_not_call_jira(tmp_path):
    _policy(tmp_path, "jira_transitions:\n  failed: '31'\n")
    scheduler = DurableScheduler(tmp_path / "state.db", parse_plan(_plan()), "DEV")
    scheduler.claim(DispatchBudget())
    with patch("codehelm.orchestrator.durable.JiraClient.from_env") as from_env:
        scheduler.settle("S1", "failed", "agent failed")
    from_env.assert_not_called()
    assert scheduler.snapshot()["S1"][0] == "failed"
    assert _events(scheduler.store.database) == [(
        "jira_transition_dry_run",
        "Dry run: would post transition 31 on DEV-9. Jira is not called.",
    )]


def test_execute_posts_and_a_failure_is_kept(tmp_path):
    _policy(tmp_path, "jira_transitions:\n  failed: '31'\njira_transition_execute: true\n")
    scheduler = DurableScheduler(tmp_path / "state.db", parse_plan(_plan()), "DEV")
    scheduler.claim(DispatchBudget())
    with patch("codehelm.orchestrator.durable.JiraClient.from_env") as from_env:
        from_env.return_value.transition.return_value = {}
        scheduler.settle("S1", "failed", "agent failed")
    from_env.return_value.transition.assert_called_once_with("DEV-9", "31")
    assert _events(scheduler.store.database)[-1][0] == "jira_transition_posted"

    scheduler.retry("S1", DispatchBudget())
    scheduler.claim(DispatchBudget())
    with patch("codehelm.orchestrator.durable.JiraClient.from_env") as from_env:
        from_env.return_value.transition.side_effect = JiraError("Jira returned HTTP 400")
        with pytest.raises(JiraTransitionError, match="HTTP 400"):
            scheduler.settle("S1", "failed", "again")
    assert scheduler.snapshot()["S1"][0] == "failed"
    assert _events(scheduler.store.database)[-1] == (
        "jira_transition_failed",
        "Jira transition 31 on DEV-9 failed: Jira returned HTTP 400",
    )


def test_missing_key_and_unmapped_state_do_not_call_jira(tmp_path):
    _policy(tmp_path, "jira_transitions:\n  failed: '31'\njira_transition_execute: true\n")
    scheduler = DurableScheduler(tmp_path / "state.db", parse_plan(_plan(None)), "DEV")
    scheduler.claim(DispatchBudget())
    with patch("codehelm.orchestrator.durable.JiraClient.from_env") as from_env:
        with pytest.raises(JiraTransitionError, match="no issue key"):
            scheduler.settle("S1", "failed", "agent failed")
    from_env.assert_not_called()
    assert _events(scheduler.store.database)[-1][0] == "jira_transition_failed"

    scheduler.retry("S1", DispatchBudget())
    scheduler.claim(DispatchBudget())
    with patch("codehelm.orchestrator.durable.JiraClient.from_env") as from_env:
        scheduler.settle("S1", "awaiting_validation")
    from_env.assert_not_called()
    assert scheduler.snapshot()["S1"][0] == "awaiting_validation"
    assert [row[0] for row in _events(scheduler.store.database)].count("jira_transition_failed") == 1


def test_completion_dry_run_uses_the_mapped_id(tmp_path):
    _policy(tmp_path, "jira_transitions:\n  completed: '71'\n")
    plan = {
        "epic_title": "Feature", "epic_description": "Build it",
        "stories": [
            {"id": "S1", "title": "First", "description": "First", "jira_key": "DEV-4",
             "acceptance_criteria": ["Works"], "dependencies": []},
        ],
    }
    scheduler = DurableScheduler(tmp_path / "state.db", parse_plan(json.dumps(plan)), "DEV")
    scheduler.claim(DispatchBudget())
    scheduler.finish("S1", "awaiting_validation")
    with sqlite3.connect(scheduler.store.database) as db:
        db.execute("UPDATE dispatches SET state='ready_for_pr' WHERE story_id='S1'")
    scheduler.store.transition("DEV-S1-A1", "pr_created", error="https://github.com/acme/widgets/pull/9")
    scheduler.store.record_evaluation("DEV-S1-A1", 1, "S1", 1, "ready_for_pr", "[]", False)
    with patch("codehelm.orchestrator.durable.github_pull_request_merged"), \
         patch("codehelm.orchestrator.durable.JiraClient.from_env") as from_env:
        scheduler.complete("S1", human_verified_merge=True)
    from_env.assert_not_called()
    assert scheduler.snapshot()["S1"][0] == "completed"
    assert _events(scheduler.store.database)[-1][1].startswith("Dry run: would post transition 71 on DEV-4")
