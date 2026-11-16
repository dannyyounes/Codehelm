"""Human documentation exceptions are explicit and do not waive other gates."""
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codehelm.agents.documentation import DocumentationResult
from codehelm.cli.main import app
from codehelm.orchestrator.lifecycle import LifecycleEngine, TaskStore
from codehelm.quality.contract import QualityPolicy, assemble_evidence, evaluate
from codehelm.quality.gates import GateResult


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


def record_other_gates(store: TaskStore, task_id: str = "DEV-1", attempt: int = 1) -> None:
    store.record_evidence(task_id, "tests", "command_gate", passed=True,
                          attempt=attempt, exit_code=0, detail="pytest")
    store.record_evidence(task_id, "security", "command_gate", passed=True,
                          attempt=attempt, exit_code=0, detail="bandit")
    store.record_evidence(task_id, "review", "independent_review", passed=True,
                          attempt=attempt, identity="codex", detail="APPROVED")
    store.record_evidence(task_id, "architecture", "independent_review", passed=True,
                          attempt=attempt, identity="codex", detail="APPROVED")
    store.record_evidence(task_id, "acceptance", "human", passed=True,
                          attempt=attempt, detail="Works")


def scored(store, repo, task_id: str = "DEV-1", attempt: int = 1):
    assembled = assemble_evidence(
        store, task_id, attempt, QualityPolicy(), repo, ["Works"], "S1", "claude")
    return assembled, evaluate(QualityPolicy(), assembled)


def test_human_docs_exception_verifies_documentation(tmp_path):
    store, repo = workspace(tmp_path)
    record_other_gates(store)
    store.record_evidence(
        "DEV-1", "docs", "human", passed=True, detail=" README already covers this ",
        identity=" dannyyounes ")
    assembled, decision = scored(store, repo)
    assert assembled.docs_verified is True
    assert "documentation_not_verified" not in decision.failures
    assert decision.status == "ready_for_pr"


def test_failed_documentation_agent_still_blocks_without_an_exception(tmp_path):
    store, repo = workspace(tmp_path)
    record_other_gates(store)
    store.record_evidence(
        "DEV-1", "docs", "documentation_agent", passed=False,
        detail="No documentation changes detected", identity="claude")
    assembled, decision = scored(store, repo)
    assert assembled.docs_verified is False
    assert "documentation_not_verified" in decision.failures
    assert decision.status == "blocked"


def test_human_docs_exception_on_another_attempt_does_not_count(tmp_path):
    store, repo = workspace(tmp_path)
    record_other_gates(store, attempt=1)
    store.record_evidence(
        "DEV-1", "docs", "documentation_agent", passed=False, attempt=1,
        detail="No documentation changes detected")
    store.record_evidence(
        "DEV-1", "docs", "human", passed=True, attempt=2,
        detail="README already covers this", identity="dannyyounes")
    assembled, decision = scored(store, repo, attempt=1)
    assert assembled.docs_verified is False
    assert "documentation_not_verified" in decision.failures
    later, later_decision = scored(store, repo, attempt=2)
    assert later.docs_verified is True
    assert "documentation_not_verified" not in later_decision.failures
    assert later_decision.status == "blocked"


def test_record_evidence_rejects_empty_human_docs_and_agent_source(tmp_path):
    store = TaskStore(tmp_path / "state.db")
    store.initialize()
    with pytest.raises(ValueError, match="reason and identity"):
        store.record_evidence(
            "DEV-1", "docs", "human", passed=True, detail="   ", identity="dannyyounes")
    with pytest.raises(ValueError, match="reason and identity"):
        store.record_evidence(
            "DEV-1", "docs", "human", passed=True, detail="already documented", identity="  ")
    with pytest.raises(ValueError, match="reason and identity"):
        store.record_evidence(
            "DEV-1", "docs", "human", passed=False, detail="already documented",
            identity="dannyyounes")
    with pytest.raises(ValueError, match="Untrusted"):
        store.record_evidence(
            "DEV-1", "docs", "agent", passed=True, detail="no change needed", identity="claude")
    assert store.evidence_for("DEV-1", 1) == []


def test_docs_exception_dry_run_does_not_call_github(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = tmp_path / ".codehelm"
    state.mkdir()
    store = TaskStore(state / "state.db")
    store.initialize()
    with patch("codehelm.cli.main.github_user_login") as lookup:
        result = CliRunner().invoke(app, [
            "docs-exception", "DEV-1", "--reason", "already documented",
            "--approver", "dannyyounes",
        ])
    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert not lookup.called
    assert store.evidence_for("DEV-1", 1) == []


def test_docs_exception_mismatch_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = tmp_path / ".codehelm"
    state.mkdir()
    store = TaskStore(state / "state.db")
    store.initialize()
    with patch("codehelm.cli.main.github_user_login", return_value="dannyyounes"):
        result = CliRunner().invoke(app, [
            "docs-exception", "DEV-1", "--reason", "already documented",
            "--approver", "someone-else", "--execute",
        ])
    text = result.output + (result.stderr or "")
    assert result.exit_code == 1
    assert "does not match" in text
    assert store.evidence_for("DEV-1", 1) == []


def test_docs_exception_empty_reason_does_not_call_github(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = tmp_path / ".codehelm"
    state.mkdir()
    store = TaskStore(state / "state.db")
    store.initialize()
    with patch("codehelm.cli.main.github_user_login") as lookup:
        result = CliRunner().invoke(app, [
            "docs-exception", "DEV-1", "--reason", "   ",
            "--approver", "dannyyounes", "--execute",
        ])
    text = result.output + (result.stderr or "")
    assert result.exit_code == 1
    assert "nonempty reason" in text
    assert not lookup.called
    assert store.evidence_for("DEV-1", 1) == []


def test_docs_exception_match_records_the_github_login(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("codehelm.cli.main.github_user_login", return_value="dannyyounes"):
        result = CliRunner().invoke(app, [
            "docs-exception", "PLAN-S1-A2", "--reason", " already documented ",
            "--approver", " dannyyounes ", "--execute",
        ])
    assert result.exit_code == 0, result.output
    store = TaskStore(tmp_path / ".codehelm" / "state.db")
    rows = store.evidence_for("PLAN-S1-A2", 2)
    assert [(row.gate, row.source, row.passed, row.detail, row.identity, row.attempt)
            for row in rows] == [
        ("docs", "human", True, "already documented", "dannyyounes", 2)]
    assert store.evidence_for("PLAN-S1-A2", 1) == []


def test_document_skips_agent_when_exception_exists(tmp_path):
    repo = make_repo(tmp_path / "repo")
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    engine.store.transition(
        "DEV-1", "awaiting_documentation", provider="codex", workspace=str(repo))
    engine.store.record_evidence(
        "DEV-1", "docs", "human", passed=True, detail="README already documents the command",
        identity="dannyyounes")
    with patch("codehelm.orchestrator.lifecycle.run_documentation_agent") as agent:
        assert engine.document("DEV-1", "claude")
    agent.assert_not_called()
    assert engine.store.get("DEV-1").status == "awaiting_post_docs_validation"
    rows = engine.store.evidence_for("DEV-1", 1)
    assert [(row.gate, row.source, row.passed, row.identity) for row in rows] == [
        ("docs", "human", True, "dannyyounes")]


def test_document_still_fails_when_the_agent_changes_nothing(tmp_path):
    repo = make_repo(tmp_path / "repo")
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    engine.store.transition(
        "DEV-1", "awaiting_documentation", provider="codex", workspace=str(repo))
    with patch("codehelm.orchestrator.lifecycle.run_documentation_agent",
               return_value=DocumentationResult(False, "No documentation changes detected")):
        assert not engine.document("DEV-1", "claude")
    assert engine.store.get("DEV-1").status == "blocked"
    rows = engine.store.evidence_for("DEV-1", 1)
    assert [(row.gate, row.source, row.passed, row.detail) for row in rows] == [
        ("docs", "documentation_agent", False, "No documentation changes detected")]


def test_quality_wave_skips_agent_when_exception_exists(tmp_path):
    repo = make_repo(tmp_path / "repo")
    (repo / "src").mkdir()
    (repo / "src" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    database = tmp_path / "state.db"
    (tmp_path / "quality.yaml").write_text(
        "schema_version: 1\nrequire_acceptance_criteria: false\n", encoding="utf-8")
    engine = LifecycleEngine(repo, database)
    task_id = "DEV-S1-A1"
    engine.store.transition(task_id, "awaiting_validation", provider="claude", workspace=str(repo))
    engine.store.record_evidence(
        task_id, "docs", "human", passed=True, attempt=1,
        detail="README already documents the command", identity="dannyyounes")
    passed = GateResult(True, "APPROVED", 0)
    with patch("codehelm.orchestrator.lifecycle.run_documentation_agent") as agent, \
         patch("codehelm.orchestrator.lifecycle.command_gate", return_value=passed), \
         patch("codehelm.orchestrator.lifecycle.independent_review", return_value=passed):
        decision = engine.run_contract_gates(
            task_id, ["python", "-c", "pass"], ["python", "-c", "pass"],
            "codex", "claude", acceptance_criteria=["Works"], story_id="S1")
    agent.assert_not_called()
    docs = [row for row in engine.store.evidence_for(task_id, 1) if row.gate == "docs"]
    assert [(row.source, row.passed, row.identity, row.detail) for row in docs] == [
        ("human", True, "dannyyounes", "README already documents the command")]
    assert decision.status == "ready_for_pr"
    assert "documentation_not_verified" not in decision.failures
