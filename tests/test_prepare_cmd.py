"""prepare_cmd runs on the host after worktree creation and before the agent."""
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from codehelm.orchestrator.lifecycle import LifecycleEngine
from codehelm.providers.adapters import AgentResult
from codehelm.quality.contract import _load_architecture_cmd, load_policy
from codehelm.quality.gates import PREPARE_TIMEOUT, GateResult, host_command


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "test@example.org")
    git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "initial")
    return path


def write_policy(database: Path, extra: str = "") -> None:
    (database.parent / "quality.yaml").write_text("schema_version: 1\n" + extra, encoding="utf-8")


def prepare_yaml(script: str) -> str:
    quoted = "'" + script.replace("'", "''") + "'"
    return (
        "prepare_cmd:\n"
        f"  - {sys.executable}\n"
        "  - -c\n"
        f"  - {quoted}\n"
    )


def events(database: Path, task_id: str) -> list[str]:
    with sqlite3.connect(database) as conn:
        return [row[0] for row in conn.execute(
            "SELECT status FROM task_events WHERE task_id=? ORDER BY id", (task_id,))]


def test_prepare_cmd_validation_matches_architecture_cmd(tmp_path):
    path = tmp_path / "quality.yaml"
    path.write_text("schema_version: 1\n", encoding="utf-8")
    assert load_policy(path).prepare_cmd == ()
    path.write_text("schema_version: 1\nprepare_cmd:\n", encoding="utf-8")
    assert load_policy(path).prepare_cmd == ()
    path.write_text("schema_version: 1\nprepare_cmd: null\n", encoding="utf-8")
    assert load_policy(path).prepare_cmd == ()
    path.write_text("schema_version: 1\nprepare_cmd:\n  - npm\n  - ci\n", encoding="utf-8")
    assert load_policy(path).prepare_cmd == ("npm", "ci")
    with pytest.raises(ValueError) as architecture:
        _load_architecture_cmd(["bad\x00"])
    with pytest.raises(ValueError) as prepare:
        _load_architecture_cmd(["bad\x00"], "prepare_cmd")
    assert str(prepare.value) == str(architecture.value).replace("architecture_cmd", "prepare_cmd")
    for raw in ("prepare_cmd: []\n", "prepare_cmd: npm ci\n", "prepare_cmd:\n  - ''\n", "prepare_cmd:\n  - 1\n"):
        path.write_text("schema_version: 1\n" + raw, encoding="utf-8")
        with pytest.raises(ValueError, match="prepare_cmd"):
            load_policy(path)


def test_prepare_runs_on_host_before_the_agent(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    monkeypatch.setenv("CODEHELM_PREPARE_MARKER", "from-operator")
    write_policy(database, prepare_yaml(
        "import os; from pathlib import Path; "
        'assert Path("README.md").read_text(encoding="utf-8") == "base\\n"; '
        'assert not Path("agent.txt").exists(); '
        'Path("prepared.txt").write_text(os.environ["CODEHELM_PREPARE_MARKER"], encoding="utf-8")'
    ))
    seen = {}

    def agent(prompt, cwd, timeout=600):
        seen["cwd"] = Path(cwd)
        assert (Path(cwd) / "README.md").read_text(encoding="utf-8") == "base\n"
        assert (Path(cwd) / "prepared.txt").read_text(encoding="utf-8") == "from-operator"
        (Path(cwd) / "agent.txt").write_text("edited\n", encoding="utf-8")
        return AgentResult("claude", 0, "ok", "")

    engine = LifecycleEngine(repo, database)
    with patch("codehelm.orchestrator.lifecycle.CLIProvider.execute", side_effect=agent), \
         patch("codehelm.orchestrator.lifecycle.command_gate", side_effect=AssertionError("sandbox")):
        result = engine.execute("DEV-1", "Fix bug", "claude")
    assert result.exit_code == 0
    task = engine.store.get("DEV-1")
    assert task.status == "awaiting_validation"
    assert seen["cwd"] == Path(task.workspace) != repo.resolve()
    assert not (repo / "prepared.txt").exists()
    assert (seen["cwd"] / "agent.txt").read_text(encoding="utf-8") == "edited\n"
    rows = [row for row in engine.store.evidence_for("DEV-1", 1) if row.gate == "prepare"]
    assert len(rows) == 1
    assert rows[0].source == "command_gate" and rows[0].passed and rows[0].exit_code == 0


def test_failing_prepare_does_not_start_the_agent(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    write_policy(database, prepare_yaml("raise SystemExit(3)"))
    engine = LifecycleEngine(repo, database)
    with patch("codehelm.orchestrator.lifecycle.CLIProvider.execute",
               side_effect=AssertionError("agent started")):
        result = engine.execute("DEV-2", "Fix bug", "claude")
    assert result.exit_code == 3
    task = engine.store.get("DEV-2")
    assert task.status == "failed" and task.workspace
    work = Path(task.workspace)
    assert (work / "README.md").read_text(encoding="utf-8") == "base\n"
    assert not (work / "agent.txt").exists()
    rows = engine.store.evidence_for("DEV-2", 1)
    assert len(rows) == 1
    assert rows[0].gate == "prepare" and rows[0].source == "command_gate"
    assert not rows[0].passed and rows[0].exit_code == 3
    assert events(database, "DEV-2") == ["queued", "failed"]


def test_host_command_defaults_to_900s_on_the_host(tmp_path):
    assert PREPARE_TIMEOUT == 900
    completed = subprocess.CompletedProcess(["python"], 0, "", "")
    with patch("subprocess.run", return_value=completed) as run:
        result = host_command(tmp_path, ["python", "-c", "pass"])
    assert result.passed and result.exit_code == 0
    assert run.call_args.args[0] == ["python", "-c", "pass"]
    assert run.call_args.kwargs["timeout"] == 900
    assert run.call_args.kwargs["cwd"] == tmp_path
    assert run.call_args.kwargs["shell"] is False
    assert "env" not in run.call_args.kwargs
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["sleep"], 900)):
        timed = host_command(tmp_path, ["sleep", "1000"])
    assert not timed.passed and timed.exit_code is None


def test_prepare_timeout_blocks_the_agent(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    write_policy(database, "prepare_cmd:\n  - python\n  - -c\n  - pass\n")
    seen = {}

    def timed_out(workspace, command, timeout):
        seen["command"] = list(command)
        seen["timeout"] = timeout
        seen["cwd"] = workspace
        return GateResult(False, "timed out", None)

    engine = LifecycleEngine(repo, database)
    with patch("codehelm.orchestrator.lifecycle.host_command", side_effect=timed_out), \
         patch("codehelm.orchestrator.lifecycle.CLIProvider.execute",
               side_effect=AssertionError("agent started")):
        result = engine.execute("DEV-4", "Fix bug", "claude")
    assert seen["command"] == ["python", "-c", "pass"]
    assert seen["timeout"] == PREPARE_TIMEOUT == 900
    task = engine.store.get("DEV-4")
    assert Path(seen["cwd"]) == Path(task.workspace)
    assert result.exit_code == 1 and task.status == "failed"
    row = engine.store.evidence_for("DEV-4", 1)[0]
    assert row.gate == "prepare" and row.source == "command_gate"
    assert not row.passed and row.exit_code is None


@pytest.mark.parametrize("extra", [None, ""])
def test_absent_prepare_cmd_preserves_behavior(tmp_path, extra):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    if extra is not None:
        write_policy(database, extra)
    engine = LifecycleEngine(repo, database)
    with patch("codehelm.orchestrator.lifecycle.host_command",
               side_effect=AssertionError("prepare")), \
         patch("codehelm.orchestrator.lifecycle.CLIProvider.execute",
               return_value=AgentResult("claude", 0, "ok", "")):
        result = engine.execute("DEV-3", "Fix bug", "claude")
    assert result.exit_code == 0
    assert engine.store.get("DEV-3").status == "awaiting_validation"
    assert engine.store.evidence_for("DEV-3", 1) == []
    assert events(database, "DEV-3") == ["queued", "running", "awaiting_validation"]
