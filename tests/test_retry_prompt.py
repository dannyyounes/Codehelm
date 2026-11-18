"""Retry coding prompts carry the previous attempt's failing detail."""
import json
import sqlite3
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from codehelm.orchestrator.durable import DispatchBudget, DurableScheduler
from codehelm.orchestrator.lifecycle import LifecycleEngine
from codehelm.planning.planner import parse_plan


def _world(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
    plan = parse_plan(json.dumps({
        "epic_title": "Feature", "epic_description": "Build it", "stories": [{
            "id": "S1", "title": "First", "description": "First",
            "acceptance_criteria": ["Works"], "dependencies": []}]}))
    sched = DurableScheduler(tmp_path / "state.db", plan, "DEV")
    return SimpleNamespace(scheduler=sched, repo=repo, store=sched.store)


def _prompt(item, base="Implement S1"):
    engine = LifecycleEngine(item.repo, item.store.database)
    done = SimpleNamespace(exit_code=0, output="", error="")
    with patch.object(engine, "execute", return_value=done) as execute:
        status = item.scheduler.carry_story("S1", engine, prompt=base, provider="claude")
    assert status == "awaiting_validation"
    return execute.call_args.args[1]


def _next(item, budget):
    item.scheduler.finish("S1", "failed")
    item.scheduler.retry("S1", budget)
    assert item.scheduler.claim(budget) == ["S1"]


def test_first_attempt_prompt_is_unchanged(tmp_path):
    item = _world(tmp_path)
    item.scheduler.claim(DispatchBudget())
    item.store.record_evidence(
        "DEV-S1-A1", "review", "independent_review", passed=False, attempt=1,
        detail="do not include", identity="codex")
    assert _prompt(item) == "Implement S1"


def test_retry_prompt_includes_latest_failing_review_or_agent_error(tmp_path):
    budget = DispatchBudget()
    item = _world(tmp_path)
    item.scheduler.claim(budget)
    item.store.record_evidence(
        "DEV-S1-A1", "tests", "command_gate", passed=False, attempt=1,
        exit_code=1, detail="older gate failure")
    item.store.record_evidence(
        "DEV-S1-A1", "review", "independent_review", passed=False, attempt=1,
        detail="review says fix the parser", identity="codex")
    item.store.transition("DEV-S1-A1", "failed", error="agent exploded")
    _next(item, budget)
    assert _prompt(item) == (
        "Implement S1\n\nPrevious attempt feedback to address\nreview says fix the parser")
    agent = _world(tmp_path / "agent")
    agent.scheduler.claim(budget)
    agent.store.transition("DEV-S1-A1", "failed", error="agent exploded")
    _next(agent, budget)
    assert _prompt(agent) == (
        "Implement S1\n\nPrevious attempt feedback to address\nagent exploded")


def test_retry_feedback_truncation_and_unchanged_when_no_detail(tmp_path):
    budget = DispatchBudget()
    item = _world(tmp_path)
    item.scheduler.claim(budget)
    with sqlite3.connect(item.store.database) as db:
        db.execute(
            """INSERT INTO quality_evidence
               (task_id,attempt,gate,source,passed,detail,recorded_at)
               VALUES ('DEV-S1-A1',1,'review','independent_review',0,?,?)""",
            ("R" * 9000, "2026-01-01T00:00:00+00:00"))
    _next(item, budget)
    assert _prompt(item, "Base") == "Base\n\nPrevious attempt feedback to address\n" + ("R" * 8000)
    quiet = _world(tmp_path / "quiet")
    quiet.scheduler.claim(budget)
    quiet.store.record_evidence(
        "DEV-S1-A1", "review", "independent_review", passed=True, attempt=1,
        detail="APPROVED", identity="codex")
    quiet.store.record_evidence(
        "DEV-S1-A1", "tests", "command_gate", passed=False, attempt=1, exit_code=1, detail="   ")
    quiet.store.transition("DEV-S1-A1", "failed", error="  ")
    _next(quiet, budget)
    assert _prompt(quiet) == "Implement S1"


def _rejected(tmp_path, task_status="changes_requested"):
    item = _world(tmp_path)
    budget = DispatchBudget()
    assert item.scheduler.claim(budget) == ["S1"]
    item.store.record_evidence(
        "DEV-S1-A1", "review", "independent_review", passed=False, attempt=1,
        detail="review says fix the parser", identity="codex")
    _prompt(item)  # no policy: dispatch settles at awaiting_validation
    if task_status != "awaiting_validation":
        item.store.transition("DEV-S1-A1", task_status)
    return item, budget


def test_retry_accepted_after_changes_requested_without_policy(tmp_path):
    item, budget = _rejected(tmp_path)
    item.scheduler.retry("S1", budget)
    assert item.scheduler.claim(budget) == ["S1"]
    assert item.scheduler.snapshot()["S1"] == ("claimed", 2)
    from codehelm.orchestrator.durable import with_previous_attempt_feedback
    prompt = with_previous_attempt_feedback("Base", item.store, "DEV", "S1", 2)
    assert "Previous attempt feedback to address\nreview says fix the parser" in prompt


def test_retry_refused_for_awaiting_validation_with_other_task_status(tmp_path):
    import pytest
    for status in ("awaiting_validation", "reviewing"):
        item, budget = _rejected(tmp_path / status, status)
        with pytest.raises(ValueError, match="can be retried"):
            item.scheduler.retry("S1", budget)


def test_retry_attempt_limit_still_enforced_after_changes_requested(tmp_path):
    import pytest
    item, _ = _rejected(tmp_path)
    with pytest.raises(ValueError, match="attempt limit"):
        item.scheduler.retry("S1", DispatchBudget(max_attempts=1))
