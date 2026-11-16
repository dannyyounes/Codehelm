"""Quality debt accepts one matching finding on the current attempt."""
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.orchestrator.lifecycle import TaskStore
from codehelm.quality.contract import QualityPolicy, assemble_evidence, evaluate


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


def workspace(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    (repo / "src").mkdir()
    (repo / "src" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    store = TaskStore(tmp_path / "state.db")
    store.initialize()
    return store, repo


def record_verified(store: TaskStore, task_id: str = "DEV-1", attempt: int = 1) -> None:
    store.record_evidence(task_id, "tests", "command_gate", passed=True,
                          attempt=attempt, exit_code=0, detail="pytest")
    store.record_evidence(task_id, "security", "command_gate", passed=True,
                          attempt=attempt, exit_code=0, detail="bandit")
    store.record_evidence(task_id, "review", "independent_review", passed=True,
                          attempt=attempt, identity="codex", detail="APPROVED")
    store.record_evidence(task_id, "architecture", "independent_review", passed=True,
                          attempt=attempt, identity="codex", detail="APPROVED")
    store.record_evidence(task_id, "docs", "documentation_agent", passed=True,
                          attempt=attempt, detail="docs")
    store.record_evidence(task_id, "acceptance", "human", passed=True,
                          attempt=attempt, detail="Works")


def scored(store, repo, task_id: str = "DEV-1", attempt: int = 1):
    assembled = assemble_evidence(
        store, task_id, attempt, QualityPolicy(), repo, ["Works"], "S1", "claude")
    return assembled, evaluate(QualityPolicy(), assembled)


def test_unresolved_finding_blocks(tmp_path):
    store, repo = workspace(tmp_path)
    record_verified(store)
    store.record_evidence(
        "DEV-1", "finding", "independent_review", passed=False, detail="critical bug")
    assembled, decision = scored(store, repo)
    assert assembled.unresolved_findings == ["critical bug"]
    assert decision.status == "blocked"
    assert "unresolved_findings" in decision.failures


def test_debt_for_exact_finding_removes_only_that_finding(tmp_path):
    store, repo = workspace(tmp_path)
    record_verified(store)
    store.record_evidence(
        "DEV-1", "finding", "independent_review", passed=False, detail=" critical bug ")
    store.record_evidence(
        "DEV-1", "finding", "human", passed=False, detail="style nit")
    store.record_evidence(
        "DEV-1", "quality_debt", "human", passed=True,
        detail="critical bug", identity="fix tracked in the follow-up")
    store.record_evidence(
        "DEV-1", "quality_debt", "human", passed=True,
        detail="unrelated text", identity="does not match a finding")
    assembled, decision = scored(store, repo)
    assert assembled.unresolved_findings == ["style nit"]
    assert decision.status == "blocked"
    assert decision.failures == ("unresolved_findings",)
    store.record_evidence(
        "DEV-1", "quality_debt", "human", passed=True,
        detail="style nit", identity="accepted separately")
    cleared, cleared_decision = scored(store, repo)
    assert cleared.unresolved_findings == []
    assert "unresolved_findings" not in cleared_decision.failures
    assert cleared_decision.status == "ready_for_pr"


def test_debt_on_another_attempt_does_not_count(tmp_path):
    store, repo = workspace(tmp_path)
    record_verified(store, attempt=1)
    store.record_evidence(
        "DEV-1", "finding", "independent_review", passed=False, attempt=1, detail="critical bug")
    store.record_evidence(
        "DEV-1", "quality_debt", "human", passed=True, attempt=2,
        detail="critical bug", identity="accepted on the next attempt")
    assembled, decision = scored(store, repo, attempt=1)
    assert assembled.unresolved_findings == ["critical bug"]
    assert "unresolved_findings" in decision.failures
    assert decision.status == "blocked"
    assert store.evidence_for("DEV-1", 2)[0].gate == "quality_debt"


def test_empty_reason_is_rejected(tmp_path):
    store = TaskStore(tmp_path / "state.db")
    store.initialize()
    with pytest.raises(ValueError, match="reason"):
        store.record_evidence(
            "DEV-1", "quality_debt", "human", passed=True, detail="critical bug", identity="")
    with pytest.raises(ValueError, match="reason"):
        store.record_evidence(
            "DEV-1", "quality_debt", "human", passed=True, detail="critical bug", identity="   ")
    with pytest.raises(ValueError, match="reason"):
        store.record_evidence(
            "DEV-1", "quality_debt", "human", passed=True, detail="   ", identity="because")
    with pytest.raises(ValueError, match="reason"):
        store.record_evidence(
            "DEV-1", "quality_debt", "human", passed=False, detail="critical bug", identity="because")
    with pytest.raises(ValueError, match="Untrusted"):
        store.record_evidence(
            "DEV-1", "quality_debt", "agent", passed=True, detail="critical bug", identity="because")
    assert store.evidence_for("DEV-1", 1) == []


def test_debt_does_not_clear_a_missing_or_failing_test(tmp_path):
    store, repo = workspace(tmp_path)
    store.record_evidence(
        "DEV-1", "finding", "human", passed=False, detail="critical bug")
    store.record_evidence(
        "DEV-1", "quality_debt", "human", passed=True,
        detail="critical bug", identity="deferred; tests still required")
    missing, missing_decision = scored(store, repo)
    assert missing.unresolved_findings == []
    assert missing.test_exit_code is None
    assert "tests_not_verified" in missing_decision.failures
    assert missing_decision.status == "blocked"
    store.record_evidence(
        "DEV-1", "tests", "command_gate", passed=False, attempt=1, exit_code=1, detail="failed")
    failing, failing_decision = scored(store, repo)
    assert failing.test_exit_code == 1
    assert failing.unresolved_findings == []
    assert "tests_not_verified" in failing_decision.failures
    assert failing_decision.status == "blocked"
    assert "security_scan_not_verified" in failing_decision.failures
    assert "architecture_review_missing" in failing_decision.failures
    assert "independent_review_missing" in failing_decision.failures
    assert "documentation_not_verified" in failing_decision.failures
    assert "unverified_acceptance_criteria" in failing_decision.failures


def test_debt_does_not_waive_human_approval(tmp_path):
    store, repo = workspace(tmp_path)
    (repo / "migrations").mkdir()
    (repo / "migrations" / "001.sql").write_text("CREATE TABLE t(id INT);\n", encoding="utf-8")
    record_verified(store)
    store.record_evidence(
        "DEV-1", "finding", "independent_review", passed=False, detail="critical bug")
    store.record_evidence(
        "DEV-1", "quality_debt", "human", passed=True,
        detail="critical bug", identity="deferred")
    assembled, decision = scored(store, repo)
    assert assembled.unresolved_findings == []
    assert assembled.human_approved is False
    assert "database_migrations" in assembled.risks
    assert "unresolved_findings" not in decision.failures
    assert decision.status == "awaiting_human_approval"


def test_quality_debt_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = tmp_path / ".codehelm"
    state.mkdir()
    store = TaskStore(state / "state.db")
    store.initialize()
    result = CliRunner().invoke(
        app, ["quality-debt", "DEV-1", "--finding", "critical bug", "--reason", "later"])
    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert store.evidence_for("DEV-1", 1) == []


def test_quality_debt_execute_records_the_current_attempt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, [
        "quality-debt", "PLAN-S1-A2", "--finding", " critical bug ",
        "--reason", " tracked ", "--execute",
    ])
    assert result.exit_code == 0, result.output
    store = TaskStore(tmp_path / ".codehelm" / "state.db")
    rows = store.evidence_for("PLAN-S1-A2", 2)
    assert [(row.gate, row.source, row.passed, row.detail, row.identity, row.attempt) for row in rows] == [
        ("quality_debt", "human", True, "critical bug", "tracked", 2)]
    assert store.evidence_for("PLAN-S1-A2", 1) == []


def test_quality_debt_execute_rejects_an_empty_reason(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, [
        "quality-debt", "DEV-1", "--finding", "critical bug", "--reason", "   ", "--execute",
    ])
    text = result.output + (result.stderr or "")
    assert result.exit_code == 1
    assert "Error:" in text
    store = TaskStore(tmp_path / ".codehelm" / "state.db")
    assert store.evidence_for("DEV-1", 1) == []


def test_quality_debt_execute_reports_oserror(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("codehelm.cli.main.TaskStore") as store_cls:
        store_cls.return_value.initialize.side_effect = OSError("disk full")
        result = CliRunner().invoke(app, [
            "quality-debt", "DEV-1", "--finding", "critical bug", "--reason", "later", "--execute",
        ])
    text = result.output + (result.stderr or "")
    assert result.exit_code == 1
    assert "Error:" in text
    assert "disk full" in text
