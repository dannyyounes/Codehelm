"""Human approval counts only when it matches the workspace diff digest."""
import os
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.orchestrator.lifecycle import TaskStore
from codehelm.quality.contract import (
    QualityEvidence, QualityPolicy, assemble_evidence, evaluate, resolve_diff_base,
)
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


def test_tracked_binary_edits_do_not_share_a_digest(tmp_path):
    repo = make_repo(tmp_path / "repo")
    blob = repo / "app.bin"
    blob.write_bytes(b"\x00\x01\x02")
    git(repo, "add", "app.bin")
    git(repo, "commit", "-m", "bin")
    blob.write_bytes(b"\x00\x01\x03")
    first = diff_digest(repo)
    blob.write_bytes(b"\x00\x01\x04")
    assert first.startswith("sha256:")
    assert diff_digest(repo) != first


def _store(tmp_path):
    store = TaskStore(tmp_path / "state.db")
    store.initialize()
    return store


def _approve(store, repo):
    digest = diff_digest(repo)
    store.record_evidence(
        "T1", "human_approval", "human", passed=True, identity="danny", detail=digest)
    return digest


def _feature(path: Path) -> Path:
    repo = make_repo(path)
    git(repo, "checkout", "-b", "feature")
    return repo


def _migration(repo: Path, text: str = "CREATE TABLE t(id INT);\n") -> None:
    (repo / "migrations").mkdir(exist_ok=True)
    (repo / "migrations" / "001.sql").write_text(text, encoding="utf-8")


def test_commit_after_approval_keeps_digest_and_approval(tmp_path):
    repo = _feature(tmp_path / "repo")
    (repo / "README.md").write_text("hello\ntracked\n", encoding="utf-8")
    _migration(repo)
    store = _store(tmp_path)
    digest = _approve(store, repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "feature")
    assert diff_digest(repo) == digest
    evidence = assembled(store, repo)
    decision = decided(evidence)
    assert evidence.human_approved and evidence.approval_mismatch is False
    assert decision.status == "ready_for_pr"
    assert "approval_diff_mismatch" not in decision.failures
    _migration(repo, "CREATE TABLE t(id INT);\n-- edited\n")
    assert diff_digest(repo) != digest
    edited = assembled(store, repo)
    assert edited.human_approved is False and edited.approval_mismatch is True
    assert "approval_diff_mismatch" in decided(edited).failures
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "edit")
    assert diff_digest(repo) != digest
    assert assembled(store, repo).approval_mismatch is True


def test_untracked_file_commit_keeps_digest(tmp_path):
    repo = _feature(tmp_path / "repo")
    _migration(repo)
    store = _store(tmp_path)
    digest = _approve(store, repo)
    git(repo, "add", "migrations/001.sql")
    git(repo, "commit", "-m", "migration")
    assert diff_digest(repo) == digest
    evidence = assembled(store, repo)
    assert evidence.human_approved and evidence.approval_mismatch is False
    assert "approval_diff_mismatch" not in decided(evidence).failures
    _migration(repo, "CREATE TABLE t(id INT);\n-- later\n")
    assert diff_digest(repo) != digest and assembled(store, repo).approval_mismatch is True


def test_ignored_files_stay_out_of_the_merge_base_digest(tmp_path):
    repo = _feature(tmp_path / "repo")
    (repo / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "ignore")
    (repo / "secret.txt").write_text("token\n", encoding="utf-8")
    (repo / "kept.txt").write_text("keep\n", encoding="utf-8")
    first = diff_digest(repo)
    (repo / "secret.txt").write_text("other-token\n", encoding="utf-8")
    assert first.startswith("sha256:") and diff_digest(repo) == first
    (repo / "kept.txt").write_text("changed\n", encoding="utf-8")
    assert diff_digest(repo) != first


def test_digest_leaves_the_real_index_untouched(tmp_path):
    repo = _feature(tmp_path / "repo")
    readme = repo / "README.md"
    readme.write_text("hello\nstaged\n", encoding="utf-8")
    tracked = repo / "tracked.txt"
    tracked.write_text("same\n", encoding="utf-8")
    git(repo, "add", "README.md", "tracked.txt")
    readme.write_text("hello\nstaged\nunstaged\n", encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    # Content-identical touch: stale stat metadata makes `git status` rewrite the index.
    os.utime(tracked, (1_700_000_000, 1_700_000_000))
    index = (repo / ".git" / "index").read_bytes()
    assert diff_digest(repo).startswith("sha256:")
    assert (repo / ".git" / "index").read_bytes() == index
    assert not (repo / ".git" / "index.lock").exists()
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "-uall"],
        capture_output=True, check=True).stdout
    assert b"MM" in status and b"?? new.txt" in status


def test_no_base_falls_back_to_head_digest(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "dev")
    git(repo, "config", "user.email", "test@example.org")
    git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    (repo / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    assert resolve_diff_base(repo, "") is None
    (repo / "README.md").write_text("hello\nchanged\n", encoding="utf-8")
    (repo / "extra.txt").write_text("new\n", encoding="utf-8")
    (repo / "secret.txt").write_text("token\n", encoding="utf-8")
    store = _store(tmp_path)
    digest = _approve(store, repo)
    (repo / "secret.txt").write_text("other\n", encoding="utf-8")
    assert diff_digest(repo, "staging") == digest
    (repo / "extra.txt").write_text("edited\n", encoding="utf-8")
    assert diff_digest(repo) != digest and assembled(store, repo).approval_mismatch is True
    (repo / "extra.txt").write_text("new\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "save")
    mismatch = assembled(store, repo)
    assert diff_digest(repo) != digest
    assert mismatch.human_approved is False and mismatch.approval_mismatch is True
    assert "approval_diff_mismatch" in decided(mismatch).failures


def _submodule_lib(path: Path) -> tuple[Path, str]:
    """Library with two commits. Returns the repo and the first commit."""
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "test@example.org")
    git(path, "config", "user.name", "Test")
    (path / "lib.txt").write_text("one\n", encoding="utf-8")
    git(path, "add", "lib.txt")
    git(path, "commit", "-m", "lib")
    older = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True, check=True, text=True).stdout.strip()
    (path / "lib.txt").write_text("two\n", encoding="utf-8")
    git(path, "add", "lib.txt")
    git(path, "commit", "-m", "lib two")
    return path, older


def test_dirty_submodule_fails_closed(tmp_path):
    lib, _older = _submodule_lib(tmp_path / "lib")
    repo = make_repo(tmp_path / "repo")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", str(lib), "vendor")
    git(repo, "commit", "-m", "add vendor")
    assert diff_digest(repo).startswith("sha256:")
    (repo / "vendor" / "lib.txt").write_text("one\nedited\n", encoding="utf-8")
    assert diff_digest(repo) == ""


def test_untracked_nested_repo_fails_closed(tmp_path):
    """A directory with its own .git is not a submodule; its files are not in the gitlink."""
    repo = _feature(tmp_path / "repo")
    nested = repo / "vendor"
    nested.mkdir()
    git(nested, "init", "-b", "main")
    git(nested, "config", "user.email", "test@example.org")
    git(nested, "config", "user.name", "Test")
    (nested / "lib.txt").write_text("one\n", encoding="utf-8")
    git(nested, "add", "lib.txt")
    git(nested, "commit", "-m", "lib")
    assert diff_digest(repo) == ""
    (nested / "lib.txt").write_text("two\n", encoding="utf-8")
    assert diff_digest(repo) == ""


def test_clean_submodule_pointer_commit_keeps_digest(tmp_path):
    """A clean gitlink change (porcelain ``C`` only) stays approved across commit."""
    lib, older = _submodule_lib(tmp_path / "lib")
    repo = make_repo(tmp_path / "repo")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", str(lib), "vendor")
    git(repo, "commit", "-m", "add vendor")
    git(repo, "checkout", "-b", "feature")
    git(repo / "vendor", "checkout", older)
    store = _store(tmp_path)
    digest = _approve(store, repo)
    assert digest.startswith("sha256:")
    git(repo, "add", "vendor")
    git(repo, "commit", "-m", "move vendor pointer")
    assert diff_digest(repo) == digest
    evidence = assembled(store, repo)
    assert evidence.human_approved and evidence.approval_mismatch is False
    assert "approval_diff_mismatch" not in decided(evidence).failures


def test_merge_base_error_fails_closed(tmp_path):
    repo = make_repo(tmp_path / "repo")
    git(repo, "checkout", "--orphan", "other")
    (repo / "other.txt").write_text("other\n", encoding="utf-8")
    git(repo, "add", "other.txt")
    git(repo, "commit", "-m", "unrelated")
    assert resolve_diff_base(repo, "") == "refs/heads/main"
    assert diff_digest(repo) == ""


def test_approve_and_score_use_policy_base_branch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = tmp_path / ".codehelm"
    state.mkdir()
    (state / "quality.yaml").write_text(
        "schema_version: 1\nbase_branch: staging\n", encoding="utf-8")
    repo = make_repo(tmp_path / "repo")
    git(repo, "checkout", "-b", "staging")
    (repo / "README.md").write_text("hello\nstaging\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "staging")
    git(repo, "checkout", "-b", "feature")
    _migration(repo)
    staged = diff_digest(repo, "staging")
    assert staged.startswith("sha256:") and staged != diff_digest(repo, "")
    store = TaskStore(state / "state.db")
    store.initialize()
    store.transition("DEV-1", "awaiting_human_approval", workspace=str(repo))
    with patch("codehelm.cli.main.github_user_login", return_value="dannyyounes"):
        result = CliRunner().invoke(
            app, ["quality-approve", "DEV-1", "--approver", "dannyyounes", "--execute"])
    assert result.exit_code == 0, result.output
    assert store.evidence_for("DEV-1", 1)[0].detail == staged
    scored = assemble_evidence(
        store, "DEV-1", 1, QualityPolicy(base_branch="staging"), repo, ["Works"], "S1", "claude")
    assert scored.human_approved and scored.approval_mismatch is False
    other = assemble_evidence(
        store, "DEV-1", 1, QualityPolicy(), repo, ["Works"], "S1", "claude")
    assert other.human_approved is False and other.approval_mismatch is True


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
