"""Read-only agent listing and same-attempt resume of an interrupted story."""
import json
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.orchestrator.durable import DispatchBudget, DurableScheduler
from codehelm.orchestrator.lifecycle import LifecycleEngine, TaskStore
from codehelm.planning.planner import parse_plan

runner = CliRunner()


def _plan_text() -> str:
    return json.dumps({
        "epic_title": "Feature",
        "epic_description": "Build it",
        "stories": [{
            "id": "S1",
            "title": "First",
            "description": "First story",
            "acceptance_criteria": ["Works"],
            "dependencies": [],
        }],
    })


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.org")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial")
    return path


def _prepare(tmp_path: Path, *, task_status: str | None, dispatch_state: str):
    monkey_root = tmp_path
    state = monkey_root / ".codehelm"
    state.mkdir()
    database = state / "state.db"
    plan_path = monkey_root / "plan.json"
    plan_path.write_text(_plan_text(), encoding="utf-8")
    plan = parse_plan(_plan_text())
    scheduler = DurableScheduler(database, plan, "PLAN")
    if dispatch_state != "pending":
        assert scheduler.claim(DispatchBudget()) == ["S1"]
        with sqlite3.connect(database) as db:
            db.execute(
                "UPDATE dispatches SET state=? WHERE prefix=? AND story_id=?",
                (dispatch_state, "PLAN", "S1"))
    if task_status is not None:
        scheduler.store.transition("PLAN-S1-A1", task_status, provider="claude", workspace=str(monkey_root))
    return plan_path, database, plan


def _resume_execute(plan_path: Path, repo: Path, task_id: str = "PLAN-S1-A1"):
    return runner.invoke(app, [
        "resume", task_id, "--execute", "--prefix", "PLAN",
        "--plan-file", str(plan_path), "--repo", str(repo),
    ])


def test_agents_lists_running_before_older_completed_and_respects_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = TaskStore(tmp_path / ".codehelm" / "state.db")
    store.initialize()
    store.transition("PLAN-S1-A1", "running", provider="claude")
    store.transition("PLAN-S2-A1", "awaiting_validation", provider="codex")
    store.transition("PLAN-S3-A1", "queued", provider="gemini")
    with sqlite3.connect(store.database) as db:
        db.execute("UPDATE tasks SET updated_at=? WHERE id=?", ("2024-01-01T00:00:00+00:00", "PLAN-S1-A1"))
        db.execute("UPDATE tasks SET updated_at=? WHERE id=?", ("2020-01-01T00:00:00+00:00", "PLAN-S2-A1"))
        db.execute("UPDATE tasks SET updated_at=? WHERE id=?", ("2019-01-01T00:00:00+00:00", "PLAN-S3-A1"))
    result = runner.invoke(app, ["agents"])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert [line.split()[0] for line in lines] == ["PLAN-S1-A1", "PLAN-S3-A1", "PLAN-S2-A1"]
    assert lines[0] == "PLAN-S1-A1 running claude 2024-01-01T00:00:00+00:00"
    assert "2020-01-01T00:00:00+00:00" in lines[-1]
    limited = runner.invoke(app, ["agents", "--limit", "1"])
    assert limited.exit_code == 0, limited.output
    limited_lines = [line for line in limited.output.splitlines() if line.strip()]
    assert limited_lines == ["PLAN-S1-A1 running claude 2024-01-01T00:00:00+00:00"]


def test_agents_empty_db_does_not_create_a_task(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    database = tmp_path / ".codehelm" / "state.db"
    store = TaskStore(database)
    store.initialize()
    assert store.list_recent(20) == []
    result = runner.invoke(app, ["agents"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "No tasks are recorded."
    assert store.list_recent(20) == []
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_resume_dry_run_does_not_change_dispatch_or_call_carry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan_path, database, plan = _prepare(tmp_path, task_status="awaiting_validation", dispatch_state="interrupted")
    before = DurableScheduler(database, plan, "PLAN").snapshot()
    original = DurableScheduler.carry_story

    def spy(self, *args, **kwargs):
        spy.calls += 1
        return original(self, *args, **kwargs)

    spy.calls = 0
    with patch("codehelm.orchestrator.durable.DurableScheduler.carry_story", spy), \
         patch("codehelm.orchestrator.lifecycle.LifecycleEngine.execute") as execute:
        result = runner.invoke(app, ["resume", "PLAN-S1-A1", "--plan-file", str(plan_path)])
    assert result.exit_code == 0, result.output
    assert "PLAN-S1-A1" in result.output
    assert "Dry run" in result.output
    assert spy.calls == 0
    assert execute.call_count == 0
    assert DurableScheduler(database, plan, "PLAN").snapshot() == before
    assert before["S1"] == ("interrupted", 1)


@pytest.mark.parametrize("status", ["queued", "running", "failed"])
def test_resume_execute_refuses_to_relaunch_agent(tmp_path, monkeypatch, status):
    monkeypatch.chdir(tmp_path)
    plan_path, database, plan = _prepare(tmp_path, task_status=status, dispatch_state="interrupted")
    repo = _make_repo(tmp_path / "repo")
    with patch("codehelm.orchestrator.lifecycle.LifecycleEngine.execute") as execute:
        result = _resume_execute(plan_path, repo)
    assert result.exit_code == 1
    assert "will not be relaunched" in result.output
    assert execute.call_count == 0
    assert DurableScheduler(database, plan, "PLAN").snapshot()["S1"] == ("interrupted", 1)


def test_resume_execute_reclaims_awaiting_validation_without_executing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan_path, database, plan = _prepare(
        tmp_path, task_status="awaiting_validation", dispatch_state="interrupted")
    repo = _make_repo(tmp_path / "repo")
    original = DurableScheduler.carry_story
    seen = {}

    def spy(self, story_id, engine, **kwargs):
        seen["story_id"] = story_id
        seen["engine"] = engine
        seen["kwargs"] = kwargs
        return original(self, story_id, engine, **kwargs)

    with patch("codehelm.orchestrator.durable.DurableScheduler.carry_story", spy), \
         patch("codehelm.orchestrator.lifecycle.LifecycleEngine.execute") as execute:
        result = _resume_execute(plan_path, repo)
    assert result.exit_code == 0, result.output
    assert "S1: awaiting_validation" in result.output
    assert seen["story_id"] == "S1"
    assert isinstance(seen["engine"], LifecycleEngine)
    assert execute.call_count == 0
    again = DurableScheduler(database, plan, "PLAN")
    assert again.snapshot()["S1"] == ("awaiting_validation", 1)
    assert again.store.get("PLAN-S1-A1") is not None
    assert again.store.get("PLAN-S1-A2") is None


@pytest.mark.parametrize("task_id", [
    "DEV-123",
    "PLAN-S1",
    "PLAN-S1-A",
    "PLAN-S1-A0",
    "PLAN-S1-A1-extra",
    "not-a-task",
])
def test_resume_rejects_task_id_that_is_not_prefix_story_attempt(tmp_path, monkeypatch, task_id):
    monkeypatch.chdir(tmp_path)
    plan_path, database, plan = _prepare(tmp_path, task_status=None, dispatch_state="pending")
    before = DurableScheduler(database, plan, "PLAN").snapshot()
    dry = runner.invoke(app, ["resume", task_id])
    assert dry.exit_code == 1
    assert "PREFIX-STORY-An" in dry.output
    executed = runner.invoke(app, [
        "resume", task_id, "--execute", "--prefix", "PLAN", "--plan-file", str(plan_path),
    ])
    assert executed.exit_code == 1
    assert "PREFIX-STORY-An" in executed.output
    assert DurableScheduler(database, plan, "PLAN").snapshot() == before


def test_resume_does_not_move_a_pending_story_or_increment_attempts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan_path, database, plan = _prepare(
        tmp_path, task_status="awaiting_validation", dispatch_state="pending")
    repo = _make_repo(tmp_path / "repo")
    with patch("codehelm.orchestrator.lifecycle.LifecycleEngine.execute") as execute, \
         patch("codehelm.orchestrator.durable.DurableScheduler.carry_story") as carry:
        result = _resume_execute(plan_path, repo)
    assert result.exit_code == 1
    assert "not interrupted" in result.output
    assert execute.call_count == 0
    assert carry.call_count == 0
    assert DurableScheduler(database, plan, "PLAN").snapshot()["S1"] == ("pending", 0)


def test_resume_execute_refuses_pr_created(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan_path, database, plan = _prepare(tmp_path, task_status="pr_created", dispatch_state="interrupted")
    repo = _make_repo(tmp_path / "repo")
    with patch("codehelm.orchestrator.lifecycle.LifecycleEngine.execute") as execute:
        result = _resume_execute(plan_path, repo)
    assert result.exit_code == 1
    assert execute.call_count == 0
    assert DurableScheduler(database, plan, "PLAN").snapshot()["S1"] == ("interrupted", 1)
