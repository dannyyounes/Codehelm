"""Human approval counts only when it matches the workspace diff digest."""
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.orchestrator.lifecycle import TaskStore
from codehelm.quality.contract import QualityEvidence, QualityPolicy, assemble_evidence, evaluate
from codehelm.quality.diffdigest import diff_digest


def good(**changes):
    args = dict(
        story_id="S1", acceptance_criteria=["login works"], verified_criteria=["login works"],
        changed_files=["src/login.py"], changed_lines=25, test_exit_code=0, security_exit_code=0,
        architecture_approved=True, reviewer_approved=True, reviewer_identity="codex",
        implementer_identity="claude", docs_verified=True,
    )
    args.update(changes)
    return QualityEvidence(**args)


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


def risky_workspace(path: Path) -> Path:
    """Tracked edit plus an untracked migration, so the digest must cover both."""
    repo = make_repo(path)
    (repo / "README.md").write_text("hello\ntracked\n", encoding="utf-8")
    (repo / "migrations").mkdir()
    (repo / "migrations" / "001.sql").write_text("CREATE TABLE t(id INT);\n", encoding="utf-8")
    return repo


def assembled(store, repo):
    return assemble_evidence(
        store, "T1", 1, QualityPolicy(), repo, ["Works"], "S1", "claude")


def decided(evidence):
    return evaluate(QualityPolicy(), good(
        risks=evidence.risks,
        human_approved=evidence.human_approved,
        approval_mismatch=evidence.approval_mismatch,
    ))


def test_matching_digest_counts_as_human_approval(tmp_path):
    repo = risky_workspace(tmp_path / "repo")
    store = TaskStore(tmp_path / "state.db")
    store.initialize()
    digest = diff_digest(repo)
    assert digest.startswith("sha256:") and len(digest) == len("sha256:") + 64
    store.record_evidence(
        "T1", "human_approval", "human", passed=True, identity="danny", detail=digest)
    evidence = assembled(store, repo)
    assert "database_migrations" in evidence.risks
    assert evidence.human_approved
    assert evidence.approval_mismatch is False
    decision = decided(evidence)
    assert decision.requires_human
    assert decision.status == "ready_for_pr"
    assert "approval_diff_mismatch" not in decision.failures


def test_changed_file_after_approval_mismatches(tmp_path):
    repo = risky_workspace(tmp_path / "repo")
    store = TaskStore(tmp_path / "state.db")
    store.initialize()
    store.record_evidence(
        "T1", "human_approval", "human", passed=True, identity="danny",
        detail=diff_digest(repo))
    (repo / "migrations" / "001.sql").write_text(
        "CREATE TABLE t(id INT);\n-- edited\n", encoding="utf-8")
    evidence = assembled(store, repo)
    assert evidence.human_approved is False
    assert evidence.approval_mismatch is True
    decision = decided(evidence)
    assert "approval_diff_mismatch" in decision.failures
    assert decision.status == "blocked"


def test_empty_digest_does_not_count(tmp_path):
    repo = risky_workspace(tmp_path / "repo")
    store = TaskStore(tmp_path / "state.db")
    store.initialize()
    with sqlite3.connect(store.database) as db:
        db.execute(
            """INSERT INTO quality_evidence
               (task_id,attempt,gate,source,passed,exit_code,detail,identity,recorded_at)
               VALUES ('T1',1,'human_approval','human',1,NULL,'','danny','2020-01-01T00:00:00+00:00')""")
    evidence = assembled(store, repo)
    assert diff_digest(repo)
    assert evidence.human_approved is False
    assert evidence.approval_mismatch is False
    decision = decided(evidence)
    assert decision.status == "awaiting_human_approval"
    assert "approval_diff_mismatch" not in decision.failures


def test_missing_approval_still_awaits_human(tmp_path):
    repo = risky_workspace(tmp_path / "repo")
    store = TaskStore(tmp_path / "state.db")
    store.initialize()
    evidence = assembled(store, repo)
    assert evidence.human_approved is False
    assert evidence.approval_mismatch is False
    decision = decided(evidence)
    assert decision.status == "awaiting_human_approval"
    assert decision.requires_human
    assert "approval_diff_mismatch" not in decision.failures


def test_record_evidence_rejects_human_approval_without_detail(tmp_path):
    store = TaskStore(tmp_path / "state.db")
    store.initialize()
    with pytest.raises(ValueError, match="diff digest"):
        store.record_evidence("T1", "human_approval", "human", passed=True, identity="danny")
    with pytest.raises(ValueError, match="diff digest"):
        store.record_evidence(
            "T1", "human_approval", "human", passed=True, identity="danny", detail="   ")
    with pytest.raises(ValueError, match="identity"):
        store.record_evidence(
            "T1", "human_approval", "human", passed=True, detail="sha256:abc")
    assert store.evidence_for("T1", 1) == []


def test_digest_fails_closed_and_ignores_ignored_files(tmp_path):
    assert diff_digest(None) == ""
    assert diff_digest(tmp_path / "missing") == ""
    note = tmp_path / "note.txt"
    note.write_text("not a directory\n", encoding="utf-8")
    assert diff_digest(note) == ""
    assert diff_digest(tmp_path) == ""
    repo = make_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "ignore")
    (repo / "secret.txt").write_text("token\n", encoding="utf-8")
    first = diff_digest(repo)
    assert first.startswith("sha256:")
    (repo / "secret.txt").write_text("other-token\n", encoding="utf-8")
    assert diff_digest(repo) == first
    (repo / "my file.txt").write_bytes(b"abc\xff")
    with_space = diff_digest(repo)
    assert with_space != first
    (repo / "my file.txt").write_bytes(b"abd\xff")
    assert diff_digest(repo) != with_space
    outside = tmp_path / "secret"
    outside.write_text("TOP SECRET\n", encoding="utf-8")
    link = repo / "link.txt"
    link.symlink_to(outside)
    linked = diff_digest(repo)
    assert linked.startswith("sha256:")
    outside.write_text("CHANGED SECRET\n", encoding="utf-8")
    assert diff_digest(repo) == linked


def test_approve_without_workspace_does_not_record(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".codehelm").mkdir()
    store = TaskStore(tmp_path / ".codehelm" / "state.db")
    store.initialize()
    store.transition("DEV-1", "awaiting_human_approval")
    with patch("codehelm.cli.main.github_user_login", return_value="dannyyounes"):
        result = CliRunner().invoke(
            app, ["quality-approve", "DEV-1", "--approver", "dannyyounes", "--execute"])
    assert result.exit_code == 1
    assert "digest" in result.output
    assert store.evidence_for("DEV-1", 1) == []
