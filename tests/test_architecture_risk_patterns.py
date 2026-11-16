"""Optional architecture command and content risk patterns."""
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from codehelm.orchestrator.lifecycle import LifecycleEngine, TaskStore
from codehelm.quality.contract import (
    QualityEvidence, QualityPolicy, assemble_evidence, evaluate, load_policy, _risks_for,
)
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


def passing(**changes):
    args = dict(
        story_id="S1", acceptance_criteria=["Works"], verified_criteria=["Works"],
        changed_files=["src/app.py"], changed_lines=2, test_exit_code=0, security_exit_code=0,
        architecture_approved=True, reviewer_approved=True, reviewer_identity="codex",
        implementer_identity="claude", docs_verified=True,
    )
    args.update(changes)
    return QualityEvidence(**args)


def test_policy_without_new_keys_loads(tmp_path):
    path = tmp_path / "quality.yaml"
    path.write_text("schema_version: 1\nmax_changed_lines: 50\n", encoding="utf-8")
    policy = load_policy(path)
    assert policy.architecture_cmd == ()
    assert policy.risk_patterns == ()
    assert policy.max_changed_lines == 50
    path.write_text("schema_version: 1\narchitecture_cmd:\n", encoding="utf-8")
    assert load_policy(path).architecture_cmd == ()
    path.write_text("schema_version: 1\nrisk_patterns:\n", encoding="utf-8")
    assert load_policy(path).risk_patterns == ()


def test_invalid_risk_regex_fails_load(tmp_path):
    path = tmp_path / "quality.yaml"
    path.write_text(
        "schema_version: 1\n"
        "risk_patterns:\n"
        "  credential_material:\n"
        "    - '('\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid regex"):
        load_policy(path)


def test_content_pattern_on_a_non_glob_path_needs_approval(tmp_path):
    repo = make_repo(tmp_path / "repo")
    source = repo / "src"
    source.mkdir()
    (source / "kept.py").write_text("SECRET_MARKER\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "kept")
    outside = tmp_path / "secret.txt"
    outside.write_text("SECRET_MARKER\n", encoding="utf-8")
    (source / "link.py").symlink_to(outside)
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    policy_path = tmp_path / "quality.yaml"
    policy_path.write_text(
        "schema_version: 1\n"
        "human_approval_risks:\n"
        "  - credential_material\n"
        "risk_patterns:\n"
        "  credential_material:\n"
        "    - SECRET_MARKER\n",
        encoding="utf-8",
    )
    policy = load_policy(policy_path)
    assert _risks_for(["src/app.py"], policy, repo) == []
    assert _risks_for(["../secret.txt", "src/link.py"], policy, repo) == []
    (source / "app.py").write_text("VALUE = 1\nSECRET_MARKER\n", encoding="utf-8")
    assert _risks_for(["src/app.py"], policy, repo) == ["credential_material"]

    store = TaskStore(tmp_path / "state.db")
    store.initialize()
    store.record_evidence("T1", "acceptance", "human", passed=True, detail="Works")
    store.record_evidence("T1", "tests", "command_gate", passed=True, exit_code=0)
    store.record_evidence("T1", "security", "command_gate", passed=True, exit_code=0)
    store.record_evidence("T1", "review", "independent_review", passed=True, identity="codex")
    store.record_evidence("T1", "architecture", "independent_review", passed=True, identity="codex")
    store.record_evidence("T1", "docs", "documentation_agent", passed=True)
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    quiet = assemble_evidence(store, "T1", 1, policy, repo, ["Works"], "S1", "claude")
    assert "credential_material" not in quiet.risks
    (source / "app.py").write_text("VALUE = 1\nSECRET_MARKER\n", encoding="utf-8")
    evidence = assemble_evidence(store, "T1", 1, policy, repo, ["Works"], "S1", "claude")
    assert evidence.risks == ["credential_material"]
    decision = evaluate(policy, evidence)
    assert decision.failures == ()
    assert decision.status == "awaiting_human_approval"
    assert decision.requires_human


def test_architecture_check_exit_gates_evaluation():
    command = QualityPolicy(architecture_cmd=("python", "-c", "pass"))
    missing = evaluate(command, passing(architecture_check_exit=None))
    failed = evaluate(command, passing(architecture_check_exit=1))
    verified = evaluate(command, passing(architecture_check_exit=0))
    still_model = evaluate(command, passing(architecture_check_exit=0, architecture_approved=False))
    empty = evaluate(QualityPolicy(), passing(architecture_check_exit=None))
    empty_failed = evaluate(QualityPolicy(), passing(architecture_check_exit=1))
    assert "architecture_check_not_verified" in missing.failures
    assert "architecture_check_not_verified" in failed.failures
    assert "architecture_check_not_verified" not in verified.failures
    assert verified.status == "ready_for_pr"
    assert "architecture_review_missing" in still_model.failures
    assert "architecture_check_not_verified" not in still_model.failures
    assert "architecture_check_not_verified" not in empty.failures
    assert "architecture_check_not_verified" not in empty_failed.failures
    review_off = QualityPolicy(
        require_architecture=False, architecture_cmd=("python", "-c", "pass"))
    skipped = evaluate(review_off, passing(architecture_check_exit=None, architecture_approved=False))
    assert "architecture_check_not_verified" not in skipped.failures
    assert "architecture_review_missing" not in skipped.failures


def test_architecture_check_pass_requires_exit_zero(tmp_path):
    store = TaskStore(tmp_path / "state.db")
    store.initialize()
    with pytest.raises(ValueError, match="exit code 0"):
        store.record_evidence(
            "T1", "architecture_check", "command_gate", passed=True, exit_code=1)


def test_architecture_command_records_command_gate(tmp_path):
    if shutil.which("unshare") is None:
        pytest.skip("unshare is not installed")
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    marker = repo / "arch-ok.txt"
    (database.parent / "quality.yaml").write_text(
        "schema_version: 1\n"
        "require_tests: false\n"
        "require_security: false\n"
        "require_review: false\n"
        "require_docs: false\n"
        "require_acceptance_criteria: false\n"
        "architecture_cmd:\n"
        f"  - {sys.executable}\n"
        "  - -c\n"
        "  - \"open('arch-ok.txt','w').write('ok')\"\n",
        encoding="utf-8",
    )
    engine = LifecycleEngine(repo, database)
    engine.store.transition("T1", "awaiting_validation", provider="claude", workspace=str(repo))
    with patch(
        "codehelm.orchestrator.lifecycle.independent_review",
        return_value=GateResult(True, "APPROVED", 0),
    ) as review:
        engine.run_contract_gates(
            "T1",
            [sys.executable, "-c", "raise SystemExit(9)"],
            [sys.executable, "-c", "open('security-ran.txt','w').write('no')"],
            "codex",
            "claude",
        )
    assert review.called
    rows = [row for row in engine.store.evidence_for("T1", 1) if row.gate == "architecture_check"]
    assert len(rows) == 1
    assert rows[0].source == "command_gate"
    assert rows[0].passed and rows[0].exit_code == 0
    assert marker.read_text(encoding="utf-8") == "ok"
    assert not (repo / "security-ran.txt").exists()
    policy = load_policy(database.parent / "quality.yaml")
    evidence = assemble_evidence(engine.store, "T1", 1, policy, repo, ["Works"], "S1", "claude")
    assert evidence.architecture_check_exit == 0
