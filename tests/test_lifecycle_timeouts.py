"""CLI timeout options for lifecycle commands keep defaults and wire through."""
import json
import re
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

from typer.main import get_command
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.orchestrator.durable import DispatchBudget, DurableScheduler
from codehelm.planning.planner import parse_plan

runner = CliRunner()


def _plan_text() -> str:
    return json.dumps({
        "epic_title": "Feature",
        "epic_description": "Build it",
        "stories": [{
            "id": "S1",
            "title": "First",
            "description": "First story",
            "acceptance_criteria": ["Works"],
            "dependencies": [],
        }],
    })


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.org")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial")
    return path


def _prepare_interrupted(root: Path):
    state = root / ".codehelm"
    state.mkdir()
    database = state / "state.db"
    plan_path = root / "plan.json"
    plan_path.write_text(_plan_text(), encoding="utf-8")
    plan = parse_plan(_plan_text())
    scheduler = DurableScheduler(database, plan, "PLAN")
    assert scheduler.claim(DispatchBudget()) == ["S1"]
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE dispatches SET state=? WHERE prefix=? AND story_id=?",
            ("interrupted", "PLAN", "S1"))
    scheduler.store.transition(
        "PLAN-S1-A1", "awaiting_validation", provider="claude", workspace=str(root))
    return plan_path


def _durable_args(plan: Path, repo: Path, *extra: str):
    return [
        "schedule-durable", "--plan-file", str(plan), "--prefix", "PLAN",
        "--repo", str(repo), "--reserve-tokens", "100", "--reserve-usd", "0.50",
        "--limit-tokens", "1000", "--limit-usd", "10", *extra,
    ]


def _param(command: str, name: str):
    click_app = get_command(app)
    return next(item for item in click_app.commands[command].params if item.name == name)


def test_timeout_option_defaults_and_bounds():
    for command, name, default in (
        ("schedule-durable", "agent_timeout", 600),
        ("schedule-durable", "gate_timeout", 300),
        ("resume", "agent_timeout", 600),
        ("resume", "gate_timeout", 300),
        ("validate", "timeout", 300),
        ("validate-after-docs", "timeout", 300),
        ("document", "timeout", 600),
    ):
        param = _param(command, name)
        assert param.default == default
        assert param.min == 1
        assert param.max == 7200
        help_text = re.sub(r"\x1b\[[0-9;]*m", "", runner.invoke(app, [command, "--help"]).output)
        flag = f"--{name.replace('_', '-')}"
        assert flag in help_text
        assert f"default: {default}" in help_text


def test_validate_wires_timeout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".codehelm").mkdir()
    with patch("codehelm.cli.main.LifecycleEngine") as engine_cls:
        engine_cls.return_value.validate.return_value = True
        engine_cls.return_value.store.get.return_value = type(
            "T", (), {"status": "awaiting_documentation", "error": ""})()
        custom = runner.invoke(app, [
            "validate", "DEV-1", "--reviewer", "codex", "--test-cmd", "pytest",
            "--security-cmd", "bandit", "--timeout", "900", "--execute",
        ])
        default = runner.invoke(app, [
            "validate", "DEV-1", "--reviewer", "codex", "--test-cmd", "pytest",
            "--security-cmd", "bandit", "--execute",
        ])
    assert custom.exit_code == 0, custom.output
    assert default.exit_code == 0, default.output
    assert engine_cls.return_value.validate.call_args_list[0].args[4] == 900
    assert engine_cls.return_value.validate.call_args_list[1].args[4] == 300


def test_validate_after_docs_wires_timeout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".codehelm").mkdir()
    with patch("codehelm.cli.main.LifecycleEngine") as engine_cls:
        engine_cls.return_value.validate_after_docs.return_value = True
        engine_cls.return_value.store.get.return_value = type(
            "T", (), {"status": "awaiting_pr", "error": ""})()
        custom = runner.invoke(app, [
            "validate-after-docs", "DEV-1", "--reviewer", "codex", "--test-cmd", "pytest",
            "--security-cmd", "bandit", "--timeout", "1200", "--execute",
        ])
        default = runner.invoke(app, [
            "validate-after-docs", "DEV-1", "--reviewer", "codex", "--test-cmd", "pytest",
            "--security-cmd", "bandit", "--execute",
        ])
    assert custom.exit_code == 0, custom.output
    assert default.exit_code == 0, default.output
    assert engine_cls.return_value.validate_after_docs.call_args_list[0].args[4] == 1200
    assert engine_cls.return_value.validate_after_docs.call_args_list[1].args[4] == 300


def test_document_wires_timeout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".codehelm").mkdir()
    with patch("codehelm.cli.main.LifecycleEngine") as engine_cls:
        engine_cls.return_value.document.return_value = True
        engine_cls.return_value.store.get.return_value = type(
            "T", (), {"status": "awaiting_post_docs_validation", "error": ""})()
        custom = runner.invoke(app, [
            "document", "DEV-1", "--provider", "claude", "--timeout", "1800", "--execute",
        ])
        default = runner.invoke(app, [
            "document", "DEV-1", "--provider", "claude", "--execute",
        ])
    assert custom.exit_code == 0, custom.output
    assert default.exit_code == 0, default.output
    assert engine_cls.return_value.document.call_args_list[0].args[2] == 1800
    assert engine_cls.return_value.document.call_args_list[1].args[2] == 600


def test_schedule_durable_wires_timeouts(tmp_path, monkeypatch):
    def run_once(root: Path, *extra: str):
        root.mkdir()
        monkeypatch.chdir(root)
        repo = _make_repo(root / "repo")
        plan = root / "plan.json"
        plan.write_text(_plan_text(), encoding="utf-8")
        (root / ".codehelm").mkdir()
        seen = {}

        def fake_carry(self, story_id, engine, **kwargs):
            seen.update(kwargs)
            return "awaiting_validation"

        with patch("codehelm.orchestrator.durable.DurableScheduler.carry_story", fake_carry):
            result = runner.invoke(app, _durable_args(plan, repo, *extra, "--execute"))
        assert result.exit_code == 0, result.output
        return seen

    custom = run_once(tmp_path / "custom", "--agent-timeout", "1500", "--gate-timeout", "450")
    default = run_once(tmp_path / "default")
    assert custom["agent_timeout"] == 1500
    assert custom["timeout"] == 450
    assert default["agent_timeout"] == 600
    assert default["timeout"] == 300


def test_resume_wires_timeouts(tmp_path, monkeypatch):
    def run_once(root: Path, *extra: str):
        root.mkdir()
        monkeypatch.chdir(root)
        plan_path = _prepare_interrupted(root)
        repo = _make_repo(root / "repo")
        seen = {}

        def fake_carry(self, story_id, engine, **kwargs):
            seen.update(kwargs)
            return "awaiting_validation"

        with patch("codehelm.orchestrator.durable.DurableScheduler.carry_story", fake_carry):
            result = runner.invoke(app, [
                "resume", "PLAN-S1-A1", "--execute", "--prefix", "PLAN",
                "--plan-file", str(plan_path), "--repo", str(repo), *extra,
            ])
        assert result.exit_code == 0, result.output
        return seen

    custom = run_once(tmp_path / "custom", "--agent-timeout", "2100", "--gate-timeout", "700")
    default = run_once(tmp_path / "default")
    assert custom["agent_timeout"] == 2100
    assert custom["timeout"] == 700
    assert default["agent_timeout"] == 600
    assert default["timeout"] == 300
