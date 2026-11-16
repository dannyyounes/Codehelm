"""Oversized-change justification records the authenticated GitHub login."""
import sqlite3
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


def test_justify_dry_run_does_not_call_github(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = tmp_path / ".codehelm"
    state.mkdir()
    store = TaskStore(state / "state.db")
    store.initialize()
    with patch("codehelm.cli.main.github_user_login") as lookup:
        result = CliRunner().invoke(
            app, ["quality-justify", "DEV-1", "--reason", "one rename", "--approver", "danny"])
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert not lookup.called
    assert store.evidence_for("DEV-1", 1) == []


def test_justify_mismatch_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = tmp_path / ".codehelm"
    state.mkdir()
    store = TaskStore(state / "state.db")
    store.initialize()
    with patch("codehelm.cli.main.github_user_login", return_value="dannyyounes"):
        result = CliRunner().invoke(app, [
            "quality-justify", "DEV-1", "--reason", "one rename",
            "--approver", "someone-else", "--execute",
        ])
    text = result.output + (result.stderr or "")
    assert result.exit_code == 1
    assert "does not match" in text
    assert store.evidence_for("DEV-1", 1) == []


def test_justify_records_the_matching_login(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("codehelm.cli.main.github_user_login", return_value="dannyyounes") as lookup:
        result = CliRunner().invoke(app, [
            "quality-justify", "PLAN-S1-A2", "--reason", "  one mechanical rename  ",
            "--approver", " dannyyounes ", "--execute",
        ])
    assert result.exit_code == 0, result.output
    assert lookup.called
    store = TaskStore(tmp_path / ".codehelm" / "state.db")
    rows = store.evidence_for("PLAN-S1-A2", 2)
    assert [(row.gate, row.source, row.passed, row.detail, row.identity, row.attempt) for row in rows] == [
        ("oversized_justification", "human", True, "one mechanical rename", "dannyyounes", 2)]
    assert store.evidence_for("PLAN-S1-A2", 1) == []


def test_justify_rejects_an_empty_reason_before_github(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = tmp_path / ".codehelm"
    state.mkdir()
    store = TaskStore(state / "state.db")
    store.initialize()
    with patch("codehelm.cli.main.github_user_login") as lookup:
        result = CliRunner().invoke(app, [
            "quality-justify", "DEV-1", "--reason", "   ",
            "--approver", "dannyyounes", "--execute",
        ])
    text = result.output + (result.stderr or "")
    assert result.exit_code == 1
    assert "Error:" in text
    assert not lookup.called
    assert store.evidence_for("DEV-1", 1) == []


def test_record_evidence_rejects_oversized_justification_without_identity(tmp_path):
    store = TaskStore(tmp_path / "state.db")
    store.initialize()
    with pytest.raises(ValueError, match="identity"):
        store.record_evidence(
            "T1", "oversized_justification", "human", passed=True,
            detail="one rename", identity="")
    with pytest.raises(ValueError, match="identity"):
        store.record_evidence(
            "T1", "oversized_justification", "human", passed=True,
            detail="one rename", identity="   ")
    with pytest.raises(ValueError, match="detail"):
        store.record_evidence(
            "T1", "oversized_justification", "human", passed=True,
            detail="   ", identity="dannyyounes")
    assert store.evidence_for("T1", 1) == []


def test_empty_identity_fails_evaluation(tmp_path):
    repo = make_repo(tmp_path / "repo")
    (repo / "notes").mkdir()
    (repo / "notes" / "change.txt").write_text(
        "".join(f"line {i}\n" for i in range(401)), encoding="utf-8")
    store = TaskStore(tmp_path / "state.db")
    store.initialize()
    with sqlite3.connect(store.database) as db:
        db.execute(
            """INSERT INTO quality_evidence
               (task_id,attempt,gate,source,passed,exit_code,detail,identity,recorded_at)
               VALUES ('T1',1,'oversized_justification','human',1,NULL,?,?,'2020-01-01T00:00:00+00:00')""",
            ("Split was rejected; the migration is one mechanical rename.", ""))
    with patch("codehelm.integrations.github_pr.github_user_login",
               side_effect=AssertionError("evaluation must not call GitHub")):
        evidence = assemble_evidence(
            store, "T1", 1, QualityPolicy(), repo, ["Works"], "S1", "claude")
        decision = evaluate(QualityPolicy(), evidence)
    assert evidence.changed_lines > 400
    assert evidence.oversized_justification == ""
    assert "oversized_change_requires_justification" in decision.failures


def test_failed_row_with_identity_does_not_justify(tmp_path):
    repo = make_repo(tmp_path / "repo")
    (repo / "notes").mkdir()
    (repo / "notes" / "change.txt").write_text(
        "".join(f"line {i}\n" for i in range(401)), encoding="utf-8")
    store = TaskStore(tmp_path / "state.db")
    store.initialize()
    store.record_evidence(
        "T1", "oversized_justification", "human", passed=False, attempt=1,
        detail="one mechanical rename", identity="dannyyounes")
    evidence = assemble_evidence(
        store, "T1", 1, QualityPolicy(), repo, ["Works"], "S1", "claude")
    decision = evaluate(QualityPolicy(), evidence)
    assert evidence.oversized_justification == ""
    assert "oversized_change_requires_justification" in decision.failures
