"""Resume a quality-blocked story on the same attempt, without a new coder run."""
import json
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.orchestrator.durable import DispatchBudget, DurableScheduler
from codehelm.orchestrator.lifecycle import TaskStore
from codehelm.planning.planner import parse_plan
from codehelm.quality.contract import QualityPolicy, assemble_evidence, evaluate
from codehelm.quality.gates import GateResult

runner = CliRunner()
TASK = "PLAN-S1-A1"
PLAN = json.dumps({
    "epic_title": "Feature", "epic_description": "Build it",
    "stories": [{
        "id": "S1", "title": "First", "description": "First story",
        "acceptance_criteria": ["Works"], "dependencies": [],
    }],
})


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.org")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial")
    (path / "src").mkdir()
    (path / "src" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    return path


def _setup(tmp_path: Path, *, task_status: str = "blocked", dispatch: str = "blocked", docs: bool = False,
          review: bool = True, acceptance: bool = False):
    state = tmp_path / ".codehelm"
    state.mkdir()
    policy = "schema_version: 1\nrequire_architecture: false\n"
    if not acceptance:
        policy += "require_acceptance_criteria: false\n"
    if not docs:
        policy += "require_docs: false\n"
    if not review:
        policy += "require_review: false\n"
    (state / "quality.yaml").write_text(policy, encoding="utf-8")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(PLAN, encoding="utf-8")
    plan = parse_plan(PLAN)
    scheduler = DurableScheduler(state / "state.db", plan, "PLAN")
    assert scheduler.claim(DispatchBudget()) == ["S1"]
    repo = _repo(tmp_path / "repo")
    scheduler.store.transition(TASK, task_status, provider="claude", workspace=str(repo))
    if dispatch != "claimed":
        with sqlite3.connect(state / "state.db") as db:
            db.execute("UPDATE dispatches SET state=? WHERE story_id='S1'", (dispatch,))
    return plan_path, repo, scheduler


def _evidence(store, gate: str, *, passed: bool, exit_code: int | None = 0, source: str | None = None,
              identity: str = "", detail: str = "ok") -> None:
    if source is None:
        source = "command_gate" if gate in {"tests", "security"} else "independent_review"
    store.record_evidence(
        TASK, gate, source, passed=passed, attempt=1, exit_code=exit_code,
        identity=identity, detail=detail)


def _resume(plan_path: Path, repo: Path, test_detail: str = "ok"):
    calls: list[tuple] = []

    def gate(_workspace, command, _timeout=300, **_kwargs):
        calls.append(tuple(command))
        detail = test_detail if tuple(command) == ("echo-tests",) else "ok"
        return GateResult(True, detail, 0)

    with patch("codehelm.orchestrator.lifecycle.LifecycleEngine.execute") as execute, \
         patch("codehelm.orchestrator.lifecycle.command_gate", side_effect=gate), \
         patch("codehelm.orchestrator.lifecycle.independent_review",
               return_value=GateResult(True, "APPROVED", 0)) as review, \
         patch("codehelm.orchestrator.lifecycle.run_documentation_agent") as docs:
        result = runner.invoke(app, [
            "resume", TASK, "--execute", "--prefix", "PLAN", "--reviewer", "codex",
            "--plan-file", str(plan_path), "--repo", str(repo),
            "--test-cmd", "echo-tests", "--security-cmd", "echo-security",
        ])
    return result, calls, execute, review, docs


def test_resume_blocked_reruns_failed_stages_without_a_new_coder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan_path, repo, scheduler = _setup(tmp_path)
    _evidence(scheduler.store, "tests", passed=False, exit_code=1, detail="failed")
    result, calls, execute, review, docs = _resume(plan_path, repo)
    assert result.exit_code == 0, result.output
    assert "S1: ready_for_pr" in result.output
    assert execute.call_count == 0
    assert docs.call_count == 0
    assert ("echo-tests",) in calls and ("echo-security",) in calls
    assert review.call_count == 1
    assert scheduler.snapshot()["S1"] == ("ready_for_pr", 1)
    assert scheduler.store.get("PLAN-S1-A2") is None
    latest = scheduler.store.latest_evaluation(TASK, 1)
    assert latest["status"] == "ready_for_pr"
    assert "tests_not_verified" not in latest["failures"]


def test_resume_blocked_skips_a_stage_that_already_passed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan_path, repo, scheduler = _setup(tmp_path)
    _evidence(scheduler.store, "tests", passed=True, exit_code=0, detail="pre-recorded")
    result, calls, execute, _review, _docs = _resume(plan_path, repo)
    assert result.exit_code == 0, result.output
    assert execute.call_count == 0
    assert ("echo-tests",) not in calls
    assert ("echo-security",) in calls
    tests = [row for row in scheduler.store.evidence_for(TASK, 1) if row.gate == "tests"]
    assert [row.detail for row in tests] == ["pre-recorded"]
    assert scheduler.snapshot()["S1"] == ("ready_for_pr", 1)


def test_later_passing_row_supersedes_an_earlier_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan_path, repo, scheduler = _setup(tmp_path)
    _evidence(scheduler.store, "tests", passed=False, exit_code=1, detail="failed")
    _evidence(scheduler.store, "tests", passed=True, exit_code=0, detail="passed later")
    result, calls, execute, _review, _docs = _resume(plan_path, repo)
    assert result.exit_code == 0, result.output
    assert execute.call_count == 0
    assert ("echo-tests",) not in calls
    assert "tests_not_verified" not in scheduler.store.latest_evaluation(TASK, 1)["failures"]
    tests = [row for row in scheduler.store.evidence_for(TASK, 1) if row.gate == "tests"]
    assert [row.exit_code for row in tests] == [1, 0]


def test_docs_exception_satisfies_docs_without_the_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan_path, repo, scheduler = _setup(tmp_path, docs=True)
    _evidence(scheduler.store, "tests", passed=True, exit_code=0)
    _evidence(scheduler.store, "security", passed=True, exit_code=0)
    _evidence(scheduler.store, "review", passed=True, exit_code=None, identity="codex")
    _evidence(scheduler.store, "docs", passed=False, exit_code=None, source="documentation_agent",
              detail="No documentation changes detected")
    _evidence(scheduler.store, "docs", passed=True, exit_code=None, source="human",
              identity="dannyyounes", detail="README already documents this")
    result, calls, execute, review, docs = _resume(plan_path, repo)
    assert result.exit_code == 0, result.output
    assert execute.call_count == 0
    assert docs.call_count == 0
    assert calls.count(("echo-tests",)) == 1
    assert review.call_count == 1
    latest = scheduler.store.latest_evaluation(TASK, 1)
    assert latest["status"] == "ready_for_pr"
    assert "documentation_not_verified" not in latest["failures"]
    assert scheduler.snapshot()["S1"] == ("ready_for_pr", 1)


def test_scoring_uses_the_latest_row_per_gate(tmp_path):
    store = TaskStore(tmp_path / "state.db")
    store.initialize()
    store.record_evidence(TASK, "tests", "command_gate", passed=False, attempt=1, exit_code=1)
    store.record_evidence(TASK, "tests", "command_gate", passed=True, attempt=1, exit_code=0)
    passed = assemble_evidence(store, TASK, 1, QualityPolicy(), None, ["Works"], "S1", "claude")
    assert passed.test_exit_code == 0
    assert "tests_not_verified" not in evaluate(QualityPolicy(), passed).failures
    store.record_evidence(TASK, "tests", "command_gate", passed=False, attempt=1, exit_code=2)
    failed = assemble_evidence(store, TASK, 1, QualityPolicy(), None, ["Works"], "S1", "claude")
    assert failed.test_exit_code == 2
    assert "tests_not_verified" in evaluate(QualityPolicy(), failed).failures
    store.record_evidence(
        TASK, "docs", "documentation_agent", passed=False, attempt=1,
        detail="No documentation changes detected")
    assert assemble_evidence(
        store, TASK, 1, QualityPolicy(), None, ["Works"], "S1", "claude").docs_verified is False
    store.record_evidence(
        TASK, "docs", "human", passed=True, attempt=1,
        detail="README already documents this", identity="dannyyounes")
    assert assemble_evidence(
        store, TASK, 1, QualityPolicy(), None, ["Works"], "S1", "claude").docs_verified is True
    store.record_evidence(
        TASK, "acceptance", "test_report", passed=False, attempt=1, detail="Works")
    assert "Works" not in assemble_evidence(
        store, TASK, 1, QualityPolicy(), None, ["Works"], "S1", "claude").verified_criteria
    store.record_evidence(
        TASK, "acceptance", "test_report", passed=True, attempt=1, detail="Works")
    verified = assemble_evidence(
        store, TASK, 1, QualityPolicy(), None, ["Works"], "S1", "claude")
    assert verified.verified_criteria == ["Works"]
    assert "unverified_acceptance_criteria" not in evaluate(QualityPolicy(), verified).failures


@pytest.mark.parametrize("status", ["queued", "running", "failed", "pr_created"])
def test_resume_blocked_refuses_tasks_that_must_not_continue(tmp_path, monkeypatch, status):
    monkeypatch.chdir(tmp_path)
    plan_path, repo, scheduler = _setup(tmp_path, task_status=status)
    result, _calls, execute, _review, _docs = _resume(plan_path, repo)
    assert result.exit_code == 1
    assert "will not be relaunched" in result.output
    assert execute.call_count == 0
    assert scheduler.snapshot()["S1"] == ("blocked", 1)


def test_interleaved_pass_does_not_rerun_tests_or_security(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan_path, repo, scheduler = _setup(tmp_path, review=False)
    _evidence(scheduler.store, "tests", passed=False, exit_code=1, detail="failed")
    _evidence(scheduler.store, "security", passed=True, exit_code=0, detail="kept")
    _evidence(scheduler.store, "tests", passed=True, exit_code=0, detail="passed later")
    result, calls, execute, review, _docs = _resume(plan_path, repo)
    assert result.exit_code == 0, result.output
    assert calls == [] and execute.call_count == 0 and review.call_count == 0
    rows = scheduler.store.evidence_for(TASK, 1)
    assert [row.exit_code for row in rows if row.gate == "tests"] == [1, 0]
    assert [row.detail for row in rows if row.gate == "security"] == ["kept"]
    assert scheduler.snapshot()["S1"] == ("ready_for_pr", 1)


def test_interleaved_failure_reruns_only_tests(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan_path, repo, scheduler = _setup(tmp_path, review=False)
    _evidence(scheduler.store, "tests", passed=False, exit_code=1, detail="failed")
    _evidence(scheduler.store, "security", passed=True, exit_code=0, detail="kept")
    result, calls, execute, review, _docs = _resume(plan_path, repo)
    assert result.exit_code == 0, result.output
    assert calls == [("echo-tests",)] and execute.call_count == 0 and review.call_count == 0
    rows = scheduler.store.evidence_for(TASK, 1)
    assert [row.exit_code for row in rows if row.gate == "tests"] == [1, 0]
    assert [row.detail for row in rows if row.gate == "security"] == ["kept"]
    assert scheduler.snapshot()["S1"] == ("ready_for_pr", 1)


def test_blocked_resume_passing_acceptance_report_verifies_criterion(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan_path, repo, scheduler = _setup(tmp_path, acceptance=True, review=False)
    _evidence(scheduler.store, "tests", passed=False, exit_code=1, detail="failed")
    _evidence(scheduler.store, "security", passed=True, exit_code=0, detail="kept")
    _evidence(scheduler.store, "acceptance", passed=False, source="test_report", detail="Works")
    result, calls, execute, review, _docs = _resume(
        plan_path, repo, test_detail="CODEHELM_ACCEPTANCE\tWorks\tpassed")
    assert result.exit_code == 0, result.output
    assert execute.call_count == 0 and review.call_count == 0
    assert calls == [("echo-tests",)]
    reports = [row for row in scheduler.store.evidence_for(TASK, 1) if row.gate == "acceptance"]
    assert [(row.passed, row.detail) for row in reports] == [(False, "Works"), (True, "Works")]
    latest = scheduler.store.latest_evaluation(TASK, 1)
    assert latest["status"] == "ready_for_pr"
    assert "unverified_acceptance_criteria" not in latest["failures"]
    assert scheduler.snapshot()["S1"] == ("ready_for_pr", 1)


def test_resume_still_refuses_a_failed_dispatch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan_path, repo, scheduler = _setup(tmp_path, task_status="awaiting_validation", dispatch="failed")
    result, _calls, execute, _review, _docs = _resume(plan_path, repo)
    assert result.exit_code == 1
    assert "not interrupted" in result.output
    assert execute.call_count == 0
    assert scheduler.snapshot()["S1"] == ("failed", 1)
