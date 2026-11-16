"""Gaps between the written quality contract and the commands that enforce it."""

import subprocess
from unittest.mock import patch

from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.orchestrator.lifecycle import LifecycleEngine, TaskStore
from codehelm.quality.gates import GateResult


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init")
    git("config", "user.email", "test@example.org")
    git("config", "user.name", "Test")
    (repo / "README.md").write_text("Initial\n")
    git("add", ".")
    git("commit", "-m", "initial")
    return repo


def _policy(database, command):
    (database.parent / "quality.yaml").write_text(
        "schema_version: 1\n"
        "require_acceptance_criteria: false\n"
        "require_docs: false\n"
        "architecture_cmd:\n"
        f"  - {command}\n",
        encoding="utf-8",
    )


def test_validate_after_docs_runs_architecture_cmd_before_the_model(tmp_path):
    repo = _repo(tmp_path)
    database = tmp_path / "state.db"
    _policy(database, "arch-ok")
    engine = LifecycleEngine(repo, database)
    engine.store.transition("DEV-1", "awaiting_post_docs_validation", provider="claude", workspace=str(repo))
    kinds = []

    def review(*_args, **kwargs):
        kinds.append(kwargs.get("kind", "code"))
        return GateResult(True, "APPROVED", 0)

    def gate(_workspace, command, _timeout=300, *, runner="namespace", image=""):
        return GateResult(True, "", 0)

    with patch("codehelm.orchestrator.lifecycle.independent_review", side_effect=review), patch(
        "codehelm.orchestrator.lifecycle.command_gate", side_effect=gate
    ) as ran:
        engine.validate_after_docs("DEV-1", ["python", "-c", "pass"], ["python", "-c", "pass"], "codex")
    assert kinds == ["code", "architecture"]
    assert ["arch-ok"] in [list(call.args[1]) for call in ran.call_args_list]
    rows = engine.store.evidence_for("DEV-1", 1)
    assert any(row.gate == "architecture_check" and row.passed and row.exit_code == 0 for row in rows)


def test_failing_architecture_cmd_does_not_launch_the_model_review(tmp_path):
    repo = _repo(tmp_path)
    database = tmp_path / "state.db"
    _policy(database, "arch-fail")
    engine = LifecycleEngine(repo, database)
    engine.store.transition("DEV-1", "awaiting_post_docs_validation", provider="claude", workspace=str(repo))
    kinds = []

    def review(*_args, **kwargs):
        kinds.append(kwargs.get("kind", "code"))
        return GateResult(True, "APPROVED", 0)

    def gate(_workspace, command, _timeout=300, *, runner="namespace", image=""):
        if list(command) == ["arch-fail"]:
            return GateResult(False, "failed", 1)
        return GateResult(True, "", 0)

    with patch("codehelm.orchestrator.lifecycle.independent_review", side_effect=review), patch(
        "codehelm.orchestrator.lifecycle.command_gate", side_effect=gate
    ):
        assert engine.validate_after_docs(
            "DEV-1", ["python", "-c", "pass"], ["python", "-c", "pass"], "codex") is False
    assert kinds == ["code"]
    rows = engine.store.evidence_for("DEV-1", 1)
    assert any(row.gate == "architecture_check" and not row.passed for row in rows)
    assert not any(row.gate == "architecture" for row in rows)
    assert engine.store.get("DEV-1").status == "blocked"


def test_attest_rejects_a_criterion_that_is_not_in_the_plan(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = TaskStore(tmp_path / ".codehelm" / "state.db")
    store.initialize()
    store.replace_plan_criteria("DEV-1", 1, ["Works"])
    runner = CliRunner()
    rejected = runner.invoke(app, ["quality-attest", "DEV-1", "--criterion", "Other", "--execute"])
    assert rejected.exit_code == 1
    assert "planned acceptance criteria" in rejected.output
    assert store.evidence_for("DEV-1", 1)[0].gate == "acceptance_planned"
    accepted = runner.invoke(app, ["quality-attest", "DEV-1", "--criterion", "Works", "--execute"])
    assert accepted.exit_code == 0
    assert any(row.gate == "acceptance" and row.detail == "Works" for row in store.evidence_for("DEV-1", 1))


def test_attest_without_a_plan_still_records(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = TaskStore(tmp_path / ".codehelm" / "state.db")
    store.initialize()
    result = CliRunner().invoke(app, ["quality-attest", "DEV-1", "--criterion", "Works", "--execute"])
    assert result.exit_code == 0
    assert store.evidence_for("DEV-1", 1)[0].detail == "Works"
