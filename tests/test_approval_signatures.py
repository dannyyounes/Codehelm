"""SSH and GPG approval signatures are checked again at scoring time."""
import os
import shutil
import tempfile
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.orchestrator.lifecycle import TaskStore
from codehelm.quality.contract import QualityPolicy, assemble_evidence, evaluate, load_policy
from codehelm.quality.diffdigest import diff_digest
from codehelm.quality.signatures import signature_blob


runner = CliRunner()


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
    (path / "migrations").mkdir()
    (path / "migrations" / "001.sql").write_text("CREATE TABLE t(id INT);\n", encoding="utf-8")
    return path


def _tool_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GNUPGHOME"] = str(home)
    return env


def sign_ssh(directory: Path, digest: str, identity: str = "danny") -> str:
    key = directory / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-C", identity, "-q"],
        check=True, capture_output=True)
    allowed = directory / "allowed_signers"
    allowed.write_text(
        f'{identity} namespaces="codehelm" {(key.with_suffix(".pub")).read_text(encoding="utf-8")}',
        encoding="utf-8")
    message = directory / "digest.txt"
    message.write_bytes(digest.encode("ascii"))
    subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", "codehelm", str(message)],
        check=True, capture_output=True)
    return (directory / "digest.txt.sig").read_text(encoding="utf-8")


def sign_gpg(directory: Path, digest: str, uid: str) -> str:
    if shutil.which("gpg") is None:
        pytest.skip("gpg is not installed")
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="chgpg") as temporary:
        home = Path(temporary)
        home.chmod(0o700)
        env = _tool_env(home)
        generated = subprocess.run(
            ["gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
             "--quick-generate-key", uid, "ed25519", "default", "never"],
            capture_output=True, env=env)
        if generated.returncode:
            raise AssertionError(generated.stderr.decode("utf-8", errors="replace"))
        exported = subprocess.run(
            ["gpg", "--batch", "--export", "--armor", uid],
            check=True, capture_output=True, env=env, text=True)
        (directory / "gpg-public.asc").write_text(exported.stdout, encoding="utf-8")
        message = directory / "digest.txt"
        message.write_bytes(digest.encode("ascii"))
        subprocess.run(
            ["gpg", "--batch", "--detach-sign", "--armor", "--local-user", uid,
             "--output", str(directory / "digest.asc"), str(message)],
            check=True, capture_output=True, env=env)
    return (directory / "digest.asc").read_text(encoding="utf-8")


def policy_file(root: Path, mode: str, signers_name: str) -> None:
    state = root / ".codehelm"
    state.mkdir(exist_ok=True)
    (state / "quality.yaml").write_text(
        f"schema_version: 1\napproval_signature: {mode}\napproval_signers: {signers_name}\n",
        encoding="utf-8")


def evidence(root: Path, repo: Path):
    store = TaskStore(root / ".codehelm" / "state.db")
    store.initialize()
    return store, assemble_evidence(
        store, "T1", 1, load_policy(root / ".codehelm" / "quality.yaml"),
        repo, ["Works"], "S1", "claude")


def test_ssh_signature_counts_and_a_later_edit_does_not(tmp_path):
    repo = make_repo(tmp_path / "repo")
    digest = diff_digest(repo)
    signers = tmp_path / ".codehelm" / "signers"
    signers.mkdir(parents=True)
    stored = signature_blob("ssh", sign_ssh(signers, digest))
    policy_file(tmp_path, "required", "signers")
    store, _ = evidence(tmp_path, repo)
    store.record_evidence(
        "T1", "human_approval", "human", passed=True, identity="danny",
        detail=digest, signature=stored)
    approved = assemble_evidence(
        store, "T1", 1, load_policy(tmp_path / ".codehelm" / "quality.yaml"),
        repo, ["Works"], "S1", "claude")
    assert approved.human_approved
    assert approved.approval_signature_failed is False
    (repo / "migrations" / "001.sql").write_text("CREATE TABLE t(id INT);\n-- edited\n", encoding="utf-8")
    changed = assemble_evidence(
        store, "T1", 1, load_policy(tmp_path / ".codehelm" / "quality.yaml"),
        repo, ["Works"], "S1", "claude")
    assert changed.human_approved is False
    assert changed.approval_mismatch is True
    decision = evaluate(QualityPolicy(approval_signature="required"), changed)
    assert "approval_diff_mismatch" in decision.failures


def test_required_policy_rejects_an_unsigned_matching_digest(tmp_path):
    repo = make_repo(tmp_path / "repo")
    signers = tmp_path / ".codehelm" / "signers"
    signers.mkdir(parents=True)
    policy_file(tmp_path, "required", "signers")
    store, _ = evidence(tmp_path, repo)
    store.record_evidence(
        "T1", "human_approval", "human", passed=True, identity="danny",
        detail=diff_digest(repo))
    found = assemble_evidence(
        store, "T1", 1, load_policy(tmp_path / ".codehelm" / "quality.yaml"),
        repo, ["Works"], "S1", "claude")
    assert found.human_approved is False
    assert found.approval_signature_failed is True
    decision = evaluate(load_policy(tmp_path / ".codehelm" / "quality.yaml"), found)
    assert "approval_signature_unverified" in decision.failures
    assert decision.status == "blocked"


def test_off_policy_still_accepts_an_unsigned_digest(tmp_path):
    repo = make_repo(tmp_path / "repo")
    policy_file(tmp_path, "off", "missing-signers")
    store = TaskStore(tmp_path / ".codehelm" / "state.db")
    store.initialize()
    store.record_evidence(
        "T1", "human_approval", "human", passed=True, identity="danny",
        detail=diff_digest(repo))
    found = assemble_evidence(
        store, "T1", 1, load_policy(tmp_path / ".codehelm" / "quality.yaml"),
        repo, ["Works"], "S1", "claude")
    assert found.human_approved
    assert found.approval_signature_failed is False


def test_tampered_ssh_signature_does_not_count(tmp_path):
    repo = make_repo(tmp_path / "repo")
    digest = diff_digest(repo)
    signers = tmp_path / ".codehelm" / "signers"
    signers.mkdir(parents=True)
    text = sign_ssh(signers, digest)
    stored = signature_blob("ssh", text.replace("A", "B", 1))
    policy_file(tmp_path, "off", "signers")
    store = TaskStore(tmp_path / ".codehelm" / "state.db")
    store.initialize()
    store.record_evidence(
        "T1", "human_approval", "human", passed=True, identity="danny",
        detail=digest, signature=stored)
    found = assemble_evidence(
        store, "T1", 1, load_policy(tmp_path / ".codehelm" / "quality.yaml"),
        repo, ["Works"], "S1", "claude")
    assert found.human_approved is False
    assert found.approval_signature_failed is True


def test_gpg_signature_binds_the_login_and_rejects_another_uid(tmp_path):
    repo = make_repo(tmp_path / "repo")
    digest = diff_digest(repo)
    signers = tmp_path / ".codehelm" / "signers"
    signers.mkdir(parents=True)
    stored = signature_blob("gpg", sign_gpg(signers, digest, "danny"))
    policy_file(tmp_path, "required", "signers")
    store = TaskStore(tmp_path / ".codehelm" / "state.db")
    store.initialize()
    store.record_evidence(
        "T1", "human_approval", "human", passed=True, identity="danny",
        detail=digest, signature=stored)
    found = assemble_evidence(
        store, "T1", 1, load_policy(tmp_path / ".codehelm" / "quality.yaml"),
        repo, ["Works"], "S1", "claude")
    assert found.human_approved
    store.record_evidence(
        "T1", "human_approval", "human", passed=True, identity="someone-else",
        detail=digest, signature=stored)
    # The first row still verifies, so approval remains. A lone mismatched uid does not.
    lone = TaskStore(tmp_path / "other.db")
    lone.initialize()
    lone.record_evidence(
        "T1", "human_approval", "human", passed=True, identity="someone-else",
        detail=digest, signature=stored)
    (tmp_path / ".codehelm" / "state.db").write_bytes((tmp_path / "other.db").read_bytes())
    rejected = assemble_evidence(
        TaskStore(tmp_path / ".codehelm" / "state.db"), "T1", 1,
        load_policy(tmp_path / ".codehelm" / "quality.yaml"),
        repo, ["Works"], "S1", "claude")
    assert rejected.human_approved is False
    assert rejected.approval_signature_failed is True


def test_policy_rejects_an_unknown_mode_and_a_required_signature_without_signers(tmp_path):
    path = tmp_path / "quality.yaml"
    path.write_text("schema_version: 1\napproval_signature: maybe\n", encoding="utf-8")
    with pytest.raises(ValueError, match="approval_signature"):
        load_policy(path)
    path.write_text("schema_version: 1\napproval_signature: required\n", encoding="utf-8")
    with pytest.raises(ValueError, match="approval_signers"):
        load_policy(path)
    path.write_text(
        "schema_version: 1\napproval_signature: required\napproval_signers: ../keys\n",
        encoding="utf-8")
    with pytest.raises(ValueError, match="approval_signers"):
        load_policy(path)


def test_quality_approve_records_a_verified_ssh_signature(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = make_repo(tmp_path / "repo")
    digest = diff_digest(repo)
    signers = tmp_path / ".codehelm" / "signers"
    signers.mkdir(parents=True)
    signature = signers / "approval.sig"
    signature.write_text(sign_ssh(signers, digest, "dannyyounes"), encoding="utf-8")
    (tmp_path / ".codehelm" / "quality.yaml").write_text(
        "schema_version: 1\napproval_signature: required\napproval_signers: signers\n",
        encoding="utf-8")
    store = TaskStore(tmp_path / ".codehelm" / "state.db")
    store.initialize()
    store.transition("DEV-1", "awaiting_human_approval", workspace=str(repo))
    with monkeypatch.context() as patch_context:
        patch_context.setattr("codehelm.cli.main.github_user_login", lambda: "dannyyounes")
        result = runner.invoke(app, [
            "quality-approve", "DEV-1", "--approver", "dannyyounes", "--execute",
            "--signature-file", str(signature), "--signature-kind", "ssh"])
    assert result.exit_code == 0, result.output
    row = store.evidence_for("DEV-1", 1)[0]
    assert row.detail == digest
    assert row.signature.startswith("ssh\n")
    found = assemble_evidence(
        store, "DEV-1", 1, load_policy(tmp_path / ".codehelm" / "quality.yaml"),
        repo, ["Works"], "S1", "claude")
    assert found.human_approved
