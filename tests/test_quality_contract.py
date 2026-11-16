"""Quality contract: recorded evidence, not agent-written JSON."""
import json
import sqlite3
import subprocess
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.orchestrator.durable import DispatchBudget, DurableScheduler
from codehelm.orchestrator.lifecycle import LifecycleEngine, TaskStore, STATE_SCHEMA_VERSION
from codehelm.planning.planner import parse_plan
from codehelm.providers.adapters import AgentResult
from codehelm.quality.contract import (
    ContractDecision, QualityEvidence, QualityPolicy, assess, evaluate, load_policy,
)
from codehelm.quality.gates import command_gate


def good(**changes):
    args = dict(
        story_id="S1", acceptance_criteria=["login works"], verified_criteria=["login works"],
        changed_files=["src/login.py"], changed_lines=25, test_exit_code=0, security_exit_code=0,
        architecture_approved=True, reviewer_approved=True, reviewer_identity="codex",
        implementer_identity="claude", docs_verified=True,
    )
    args.update(changes)
    return QualityEvidence(**args)


def test_decision_ready():
    assert evaluate(QualityPolicy(), good()).status == "ready_for_pr"


def test_decision_rejects_unverified_acceptance():
    assert "unverified_acceptance_criteria" in evaluate(
        QualityPolicy(), good(verified_criteria=[])).failures


def test_decision_fails_closed_when_tests_not_run():
    assert "tests_not_verified" in evaluate(QualityPolicy(), good(test_exit_code=None)).failures


def test_decision_rejects_same_reviewer():
    assert "independent_review_missing" in evaluate(
        QualityPolicy(), good(reviewer_identity="claude")).failures


def test_decision_high_risk_needs_human():
    waiting = evaluate(QualityPolicy(), good(risks=["database_migrations"]))
    assert waiting.status == "awaiting_human_approval" and waiting.requires_human
    approved = evaluate(QualityPolicy(), good(risks=["database_migrations"], human_approved=True))
    assert approved.status == "ready_for_pr"


def test_decision_oversized_needs_justification():
    assert "oversized_change_requires_justification" in evaluate(
        QualityPolicy(), good(changed_lines=401)).failures


def test_decision_unresolved_findings_block():
    assert evaluate(QualityPolicy(), good(unresolved_findings=["critical bug"])).status == "blocked"


def test_missing_policy_is_inactive(tmp_path):
    policy = load_policy(tmp_path / "quality.yaml")
    assert policy.present is False and policy.schema_version is None


def test_partial_policy_keeps_defaults(tmp_path):
    path = tmp_path / "quality.yaml"
    path.write_text("schema_version: 1\nmax_changed_lines: 50\n", encoding="utf-8")
    policy = load_policy(path)
    assert policy.present and policy.max_changed_lines == 50
    assert policy.require_tests and policy.require_security and policy.max_retries == 2
    assert "database_migrations" in policy.human_approval_risks


def test_unknown_policy_schema_and_fields_fail_closed(tmp_path):
    path = tmp_path / "quality.yaml"
    path.write_text("schema_version: 99\n", encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        load_policy(path)
    path.write_text("schema_version: 1\nnot_a_field: true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown"):
        load_policy(path)


def test_agent_evidence_source_rejected(tmp_path):
    store = TaskStore(tmp_path / "state.db")
    store.initialize()
    with pytest.raises(ValueError, match="Untrusted"):
        store.record_evidence("T1", "tests", "agent", passed=True, exit_code=0)


def test_migration_preserves_existing_rows(tmp_path):
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as db:
        db.execute("""CREATE TABLE tasks (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        db.execute("INSERT INTO tasks VALUES ('OLD', 'queued', '2020-01-01T00:00:00+00:00')")
        db.execute("""CREATE TABLE dispatches (
            prefix TEXT, story_id TEXT, state TEXT, attempts INTEGER,
            claimed_at TEXT, error TEXT)""")
        db.execute("INSERT INTO dispatches VALUES ('P', 'S1', 'pending', 0, NULL, NULL)")
    store = TaskStore(database)
    store.initialize()
    assert store.get("OLD").status == "queued"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT state FROM dispatches").fetchone()[0] == "pending"
        assert db.execute("SELECT version FROM schema_migrations").fetchone()[0] == STATE_SCHEMA_VERSION
        assert db.execute("SELECT COUNT(*) FROM quality_evidence").fetchone()[0] == 0
    store.initialize()
    assert store.get("OLD").status == "queued"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "test@example.org")
    git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "initial")
    return path


PLAN = json.dumps({
    "epic_title": "Feature", "epic_description": "Build it",
    "stories": [
        {"id": "S1", "title": "First", "description": "First",
         "acceptance_criteria": ["Works"], "dependencies": []},
        {"id": "S2", "title": "Second", "description": "Second",
         "acceptance_criteria": ["Works"], "dependencies": ["S1"]},
    ],
})


def write_policy(database: Path, extra: str = "") -> None:
    (database.parent / "quality.yaml").write_text(
        "schema_version: 1\n" + extra, encoding="utf-8")


def scheduler_for(database: Path) -> DurableScheduler:
    return DurableScheduler(database, parse_plan(PLAN), "DEV")


def test_policy_absent_keeps_legacy_transitions(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    engine = LifecycleEngine(repo, database)
    engine.store.transition("DEV-22", "awaiting_post_docs_validation",
                            provider="claude", workspace=str(repo))
    (repo / "change.txt").write_text("change\n", encoding="utf-8")
    with patch("codehelm.orchestrator.lifecycle.independent_review") as review:
        review.return_value.passed = True
        review.return_value.detail = "APPROVED"
        review.return_value.exit_code = 0
        assert engine.validate_after_docs("DEV-22", ["python", "-c", "pass"],
                                          ["python", "-c", "pass"], "codex")
    assert engine.store.get("DEV-22").status == "awaiting_pr"
    scheduler = scheduler_for(database)
    scheduler.claim(DispatchBudget())
    scheduler.finish("S1", "awaiting_validation")
    assert scheduler.apply_quality("S1") == "awaiting_validation"
    assert scheduler.snapshot()["S1"][0] == "awaiting_validation"


def test_security_failure_is_recorded_from_the_process(tmp_path):
    repo = make_repo(tmp_path / "repo")
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    engine.store.transition("DEV-9", "awaiting_validation", provider="claude", workspace=str(repo))
    assert not engine.validate(
        "DEV-9", ["python", "-c", "pass"], ["python", "-c", "raise SystemExit(2)"], "codex")
    assert engine.store.get("DEV-9").status == "blocked"
    security = [row for row in engine.store.evidence_for("DEV-9", 1) if row.gate == "security"][-1]
    assert security.source == "command_gate" and security.exit_code == 2 and not security.passed
    decision = assess(
        engine.store, "DEV-9", attempt=1, policy=QualityPolicy(), workspace=repo,
        acceptance_criteria=["Works"], story_id="DEV-9", implementer="claude")
    assert "security_scan_not_verified" in decision.failures
    assert decision.status == "blocked"


def test_rejected_independent_review_blocks(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    write_policy(database)
    (repo / "src").mkdir()
    (repo / "src" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    scheduler = scheduler_for(database)
    scheduler.claim(DispatchBudget())
    scheduler.finish("S1", "awaiting_validation")
    task_id = "DEV-S1-A1"
    scheduler.store.transition(task_id, "awaiting_validation", provider="claude", workspace=str(repo))
    engine = LifecycleEngine(repo, database)
    with patch("codehelm.quality.gates.CLIProvider") as provider:
        provider.return_value.execute.return_value = AgentResult(
            "codex", 0, "Boundary bug.\nCHANGES_REQUESTED", "")
        assert not engine.validate(task_id, ["python", "-c", "pass"],
                                  ["python", "-c", "pass"], "codex")
    assert provider.return_value.execute.called
    review = [row for row in engine.store.evidence_for(task_id, 1) if row.gate == "review"][-1]
    assert review.source == "independent_review" and not review.passed
    assert "CHANGES_REQUESTED" in review.detail
    assert engine.store.get(task_id).status == "changes_requested"
    assert scheduler.apply_quality("S1") == "blocked"
    latest = scheduler.store.latest_evaluation(task_id, 1)
    assert latest["status"] == "blocked"
    assert "independent_review_missing" in latest["failures"]


def test_missing_evidence_blocks_advancement(tmp_path):
    database = tmp_path / "state.db"
    write_policy(database)
    scheduler = scheduler_for(database)
    scheduler.claim(DispatchBudget())
    scheduler.finish("S1", "awaiting_validation")
    assert scheduler.apply_quality("S1") == "blocked"
    latest = scheduler.store.latest_evaluation("DEV-S1-A1", 1)
    assert "tests_not_verified" in latest["failures"]
    assert "security_scan_not_verified" in latest["failures"]
    assert "missing_change_evidence" in latest["failures"]
    assert scheduler.snapshot()["S1"][0] == "blocked"


def _approve_review():
    return AgentResult("codex", 0, "Checked the diff.\nAPPROVED", "")


def _document(prompt, cwd, timeout=600):
    readme = Path(cwd) / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "Documented.\n", encoding="utf-8")
    return AgentResult("claude", 0, "docs", "")


def drive_verified_gates(engine: LifecycleEngine, task_id: str) -> None:
    """Run the real gate commands. Only the model processes are stubbed."""
    with patch("codehelm.quality.gates.CLIProvider") as reviewer:
        reviewer.return_value.execute.return_value = _approve_review()
        assert engine.validate(task_id, ["python", "-c", "pass"],
                               ["python", "-c", "pass"], "codex")
    with patch("codehelm.agents.documentation.CLIProvider") as writer:
        writer.return_value.execute.side_effect = _document
        assert engine.document(task_id, "claude")
    engine.store.record_evidence(task_id, "acceptance", "human", passed=True,
                                 attempt=engine._attempt_of(task_id, None), detail="Works")
    with patch("codehelm.quality.gates.CLIProvider") as reviewer:
        reviewer.return_value.execute.return_value = _approve_review()
        engine.validate_after_docs(task_id, ["python", "-c", "pass"],
                                   ["python", "-c", "pass"], "codex")


def test_high_risk_change_requires_human_approval(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    write_policy(database)
    (repo / "src").mkdir()
    (repo / "src" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "migrations").mkdir()
    (repo / "migrations" / "001.sql").write_text("CREATE TABLE t(id INT);\n", encoding="utf-8")
    scheduler = scheduler_for(database)
    scheduler.claim(DispatchBudget())
    scheduler.finish("S1", "awaiting_validation")
    task_id = "DEV-S1-A1"
    scheduler.store.transition(task_id, "awaiting_validation", provider="claude", workspace=str(repo))
    scheduler.store.replace_plan_criteria(task_id, 1, ["Works"])
    engine = LifecycleEngine(repo, database)
    drive_verified_gates(engine, task_id)
    assert engine.store.get(task_id).status == "awaiting_human_approval"
    assert scheduler.apply_quality("S1") == "awaiting_human_approval"
    scheduler.store.record_evidence(task_id, "human_approval", "human", passed=True,
                                    attempt=1, identity="danny")
    assert scheduler.apply_quality("S1") == "ready_for_pr"
    assert scheduler.snapshot()["S1"][0] == "ready_for_pr"


def test_cli_approval_matches_github_login_before_ready_for_pr(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = make_repo(tmp_path / "repo")
    state = tmp_path / ".codehelm"
    state.mkdir()
    database = state / "state.db"
    write_policy(database)
    (repo / "src").mkdir()
    (repo / "src" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "migrations").mkdir()
    (repo / "migrations" / "001.sql").write_text("CREATE TABLE t(id INT);\n", encoding="utf-8")
    scheduler = scheduler_for(database)
    scheduler.claim(DispatchBudget())
    scheduler.finish("S1", "awaiting_validation")
    task_id = "DEV-S1-A1"
    scheduler.store.transition(task_id, "awaiting_validation", provider="claude", workspace=str(repo))
    scheduler.store.replace_plan_criteria(task_id, 1, ["Works"])
    engine = LifecycleEngine(repo, database)
    drive_verified_gates(engine, task_id)
    assert scheduler.apply_quality("S1") == "awaiting_human_approval"
    with patch("codehelm.cli.main.github_user_login", return_value="dannyyounes"):
        denied = CliRunner().invoke(
            app, ["quality-approve", task_id, "--approver", "other", "--execute"])
    assert denied.exit_code == 1
    assert scheduler.apply_quality("S1") == "awaiting_human_approval"
    with patch("codehelm.cli.main.github_user_login", return_value="dannyyounes"):
        allowed = CliRunner().invoke(
            app, ["quality-approve", task_id, "--approver", "dannyyounes", "--execute"])
    assert allowed.exit_code == 0, allowed.output
    assert scheduler.apply_quality("S1") == "ready_for_pr"
    approval = [row for row in engine.store.evidence_for(task_id, 1) if row.gate == "human_approval"]
    assert [(row.source, row.identity, row.passed) for row in approval] == [("human", "dannyyounes", True)]


def test_oversized_change_blocks_until_justification(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    write_policy(database)
    (repo / "src").mkdir()
    (repo / "src" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "notes").mkdir()
    (repo / "notes" / "change.txt").write_text("".join(f"line {i}\n" for i in range(401)), encoding="utf-8")
    scheduler = scheduler_for(database)
    scheduler.claim(DispatchBudget())
    scheduler.finish("S1", "awaiting_validation")
    task_id = "DEV-S1-A1"
    scheduler.store.transition(task_id, "awaiting_validation", provider="claude", workspace=str(repo))
    scheduler.store.replace_plan_criteria(task_id, 1, ["Works"])
    engine = LifecycleEngine(repo, database)
    drive_verified_gates(engine, task_id)
    assert engine.store.get(task_id).status == "blocked"
    latest = engine.store.latest_evaluation(task_id, 1)
    assert "oversized_change_requires_justification" in latest["failures"]
    scheduler.store.record_evidence(
        task_id, "oversized_justification", "human", passed=True, attempt=1,
        detail="Split was rejected; the migration is one mechanical rename.")
    assert scheduler.apply_quality("S1") == "ready_for_pr"


def test_interrupted_attempt_evidence_does_not_resume(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    write_policy(database)
    scheduler = scheduler_for(database)
    assert scheduler.claim(DispatchBudget()) == ["S1"]
    result = command_gate(repo, ["python", "-c", "pass"])
    scheduler.store.record_evidence(
        "DEV-S1-A1", "tests", "command_gate", passed=result.passed, attempt=1,
        exit_code=result.exit_code, detail=result.detail)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with sqlite3.connect(database) as db:
        db.execute("UPDATE dispatches SET claimed_at=? WHERE story_id='S1'", (old,))
    assert scheduler.recover_expired(DispatchBudget()) == 1
    with pytest.raises(ValueError, match="interrupted"):
        scheduler.apply_quality("S1")
    assert scheduler.snapshot()["S1"][0] == "interrupted"
    assert scheduler.store.evidence_for("DEV-S1-A1", 1)[0].gate == "tests"
    scheduler.retry("S1", DispatchBudget())
    assert scheduler.claim(DispatchBudget()) == ["S1"]
    scheduler.finish("S1", "awaiting_validation")
    assert scheduler.snapshot()["S1"] == ("awaiting_validation", 2)
    assert scheduler.apply_quality("S1") == "blocked"
    resumed = scheduler.store.latest_evaluation("DEV-S1-A2", 2)
    assert "tests_not_verified" in resumed["failures"]
    assert not [row for row in scheduler.store.evidence_for("DEV-S1-A2", 2) if row.gate == "tests"]

    (repo / "src").mkdir()
    (repo / "src" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
    task_id = "DEV-S1-A2"
    scheduler.store.transition(task_id, "awaiting_validation", provider="claude", workspace=str(repo))
    scheduler.store.replace_plan_criteria(task_id, 2, ["Works"])
    engine = LifecycleEngine(repo, database)
    drive_verified_gates(engine, task_id)
    assert scheduler.apply_quality("S1") == "ready_for_pr"
    with pytest.raises(ValueError, match="draft PR"):
        scheduler.complete("S1", human_verified_merge=True)
    scheduler.store.transition(task_id, "pr_created", error="https://github.com/example/repo/pull/4")
    scheduler.complete("S1", human_verified_merge=True)
    assert scheduler.snapshot()["S1"][0] == "completed"
    assert scheduler.claim(DispatchBudget()) == ["S2"]


def test_pr_publish_failure_keeps_ready_for_pr(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    write_policy(database)
    engine = LifecycleEngine(repo, database)
    engine.store.transition("DEV-1", "ready_for_pr", provider="claude", workspace=str(repo))
    ready = ContractDecision("ready_for_pr", (), False)
    with patch("codehelm.orchestrator.lifecycle.assess", return_value=ready), \
         patch("codehelm.orchestrator.lifecycle.publish_pr", side_effect=RuntimeError("push failed")):
        with pytest.raises(RuntimeError, match="push failed"):
            engine.create_pull_request("DEV-1", "Title", "Body")
    assert engine.store.get("DEV-1").status == "ready_for_pr"


def test_existing_cli_commands_stay_available(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(app, ["--help"]).exit_code == 0
    assert "quality-eval" in runner.invoke(app, ["--help"]).output
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert not (tmp_path / ".codehelm" / "quality.yaml").exists()
    status = runner.invoke(app, ["status"])
    assert status.exit_code == 0 and "Stored tasks: 0" in status.output
    dry = runner.invoke(app, ["validate", "DEV-1", "--reviewer", "codex",
                              "--test-cmd", "pytest", "--security-cmd", "bandit"])
    assert dry.exit_code == 0 and "Dry run" in dry.output
    pr = runner.invoke(app, ["pr-create", "DEV-1", "--title", "Title", "--body", "Body"])
    assert pr.exit_code == 0 and "Dry run" in pr.output
    inactive = runner.invoke(app, ["quality-eval", "DEV-1"])
    assert inactive.exit_code == 0 and "inactive" in inactive.output
    approve = runner.invoke(app, ["quality-approve", "DEV-1", "--approver", "danny"])
    assert approve.exit_code == 0 and "Dry run" in approve.output
    assert runner.invoke(app, ["schedule-quality", "S1"]).exit_code == 0
