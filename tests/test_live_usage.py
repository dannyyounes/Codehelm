"""Live CLI usage is recorded only when structured stdout is recognized."""
import json
import sqlite3
import subprocess
from decimal import Decimal
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.orchestrator.budgets import BudgetLedger, Limits
from codehelm.orchestrator.durable import DispatchBudget, DurableScheduler
from codehelm.orchestrator.lifecycle import LifecycleEngine
from codehelm.planning.planner import parse_plan
from codehelm.providers.adapters import AgentResult

runner = CliRunner()
LIMITS = Limits(10_000, Decimal("20"))


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_repo(path):
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "test@example.org")
    git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "initial")
    return path


def plan():
    return parse_plan(json.dumps({
        "epic_title": "Feature",
        "epic_description": "Build it",
        "stories": [{
            "id": "S1",
            "title": "First",
            "description": "First story",
            "acceptance_criteria": ["Works"],
            "dependencies": [],
        }],
    }))


def reserve(database, prefix, task_id, usd="1.50"):
    BudgetLedger(database).reserve(
        prefix, task_id, "claude", 1000, Decimal(usd), LIMITS)


def usage_row(database, prefix, task_id):
    with sqlite3.connect(database) as conn:
        return conn.execute(
            """SELECT actual_tokens, actual_usd, status, typeof(actual_usd)
               FROM budget_reservations WHERE prefix=? AND task_id=?""",
            (prefix, task_id)).fetchone()


def reservation_count(database):
    with sqlite3.connect(database) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='budget_reservations'"
        ).fetchone()
        if exists is None:
            return 0
        return conn.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0]


def claude_usage(tokens_in, tokens_out, usd=None):
    payload = {"usage": {"input_tokens": tokens_in, "output_tokens": tokens_out}}
    if usd is not None:
        payload["total_cost_usd"] = usd
    return json.dumps(payload)


def run_agent(engine, task_id, output, provider="claude"):
    with patch("codehelm.orchestrator.lifecycle.CLIProvider.execute",
               return_value=AgentResult(provider, 0, output, "")):
        return engine.execute(task_id, "Fix the bug", provider)


def test_json_stdout_with_cost_reconciles_reservation(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    reserve(database, "PLAN", "DEV-123")
    engine = LifecycleEngine(repo, database)
    result = run_agent(engine, "DEV-123", claude_usage(120, 30, "0.004"))
    assert result.exit_code == 0
    assert engine.store.get("DEV-123").status == "awaiting_validation"
    assert usage_row(database, "PLAN", "DEV-123") == (150, "0.004", "reconciled", "text")
    report = BudgetLedger(database).report("PLAN")
    assert report["accounted_tokens"] == 150
    assert report["accounted_usd"] == "0.004"
    assert report["unreconciled"] == 0


def test_json_stdout_without_usd_records_tokens_only(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    reserve(database, "PLAN", "DEV-124", usd="1.50")
    engine = LifecycleEngine(repo, database)
    run_agent(engine, "DEV-124", claude_usage(10, 5))
    actual_tokens, actual_usd, status, usd_type = usage_row(database, "PLAN", "DEV-124")
    assert actual_tokens == 15
    assert actual_usd is None
    assert actual_usd != 0
    assert actual_usd != "0"
    assert usd_type == "null"
    assert status == "reserved"
    report = BudgetLedger(database).report("PLAN")
    assert report["accounted_tokens"] == 15
    assert report["accounted_usd"] == "1.50"
    assert report["unreconciled"] == 1


def test_plain_text_stdout_does_not_change_reservation(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    reserve(database, "PLAN", "DEV-125")
    engine = LifecycleEngine(repo, database)
    run_agent(engine, "DEV-125", "Agent completed successfully")
    assert usage_row(database, "PLAN", "DEV-125") == (None, None, "reserved", "null")
    assert engine.store.get("DEV-125").status == "awaiting_validation"


def test_no_reservation_inserts_nothing(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    engine = LifecycleEngine(repo, database)
    result = run_agent(engine, "DEV-126", claude_usage(8, 2, "0.20"))
    assert result.output
    assert engine.store.get("DEV-126").status == "awaiting_validation"
    assert reservation_count(database) == 0

    scheduler = DurableScheduler(database, plan(), "DEV")
    scheduler.claim(DispatchBudget())
    with patch("codehelm.orchestrator.lifecycle.CLIProvider.execute",
               return_value=AgentResult("claude", 0, claude_usage(8, 2, "0.20"), "")):
        status = scheduler.carry_story(
            "S1", LifecycleEngine(repo, database), prompt="Implement S1", provider="claude")
    assert status == "awaiting_validation"
    assert scheduler.snapshot()["S1"] == ("awaiting_validation", 1)
    assert reservation_count(database) == 0


def test_carry_story_reconciles_only_the_matching_prefix(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    scheduler = DurableScheduler(database, plan(), "DEV")
    assert scheduler.claim(DispatchBudget()) == ["S1"]
    task_id = "DEV-S1-A1"
    reserve(database, "DEV", task_id, usd="2.00")
    reserve(database, "OTHER", task_id, usd="2.00")
    engine = LifecycleEngine(repo, database)
    with patch("codehelm.orchestrator.lifecycle.CLIProvider.execute",
               return_value=AgentResult("claude", 0, claude_usage(40, 10, "0.25"), "")):
        status = scheduler.carry_story(
            "S1", engine, prompt="Implement S1", provider="claude")
    assert status == "awaiting_validation"
    assert usage_row(database, "DEV", task_id) == (50, "0.25", "reconciled", "text")
    assert usage_row(database, "OTHER", task_id) == (None, None, "reserved", "null")


def test_engine_skips_when_more_than_one_reservation_matches(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    reserve(database, "PLAN", "DEV-127")
    reserve(database, "OTHER", "DEV-127")
    engine = LifecycleEngine(repo, database)
    run_agent(engine, "DEV-127", claude_usage(3, 4, "0.10"))
    assert usage_row(database, "PLAN", "DEV-127") == (None, None, "reserved", "null")
    assert usage_row(database, "OTHER", "DEV-127") == (None, None, "reserved", "null")


def test_usage_import_tokens_only_does_not_invent_usd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    database = tmp_path / ".codehelm" / "state.db"
    reserve(database, "PLAN", "PLAN-S1-A1", usd="1.25")
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(claude_usage(8, 2), encoding="utf-8")
    args = ["usage-import", "PLAN-S1-A1", "--provider", "claude",
            "--usage-file", str(usage_file), "--prefix", "PLAN"]
    dry = runner.invoke(app, args)
    assert dry.exit_code == 0
    assert "USD is unavailable" in dry.output
    assert "reservation is retained" in dry.output
    assert "USD 0" not in dry.output
    assert usage_row(database, "PLAN", "PLAN-S1-A1") == (None, None, "reserved", "null")
    live = runner.invoke(app, args + ["--execute"])
    assert live.exit_code == 0
    assert "USD is unavailable" in live.output
    assert "reservation is retained" in live.output
    assert "USD 0" not in live.output
    actual_tokens, actual_usd, status, usd_type = usage_row(database, "PLAN", "PLAN-S1-A1")
    assert actual_tokens == 10
    assert actual_usd is None
    assert actual_usd not in (0, "0", "0.0", "0.00")
    assert usd_type == "null"
    assert status == "reserved"
    assert BudgetLedger(database).report("PLAN")["accounted_usd"] == "1.25"


def test_usage_import_with_usd_reconciles_only_when_executed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    database = tmp_path / ".codehelm" / "state.db"
    reserve(database, "PLAN", "PLAN-S1-A1")
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(claude_usage(20, 5, "0.08"), encoding="utf-8")
    args = ["usage-import", "PLAN-S1-A1", "--provider", "claude",
            "--usage-file", str(usage_file), "--prefix", "PLAN"]
    dry = runner.invoke(app, args)
    assert dry.exit_code == 0
    assert "Add --execute to reconcile." in dry.output
    assert usage_row(database, "PLAN", "PLAN-S1-A1") == (None, None, "reserved", "null")
    live = runner.invoke(app, args + ["--execute"])
    assert live.exit_code == 0
    assert usage_row(database, "PLAN", "PLAN-S1-A1") == (25, "0.08", "reconciled", "text")


def test_negative_or_nonfinite_cost_is_rejected(tmp_path, monkeypatch):
    database = tmp_path / "state.db"
    ledger = BudgetLedger(database)
    ledger.reserve("P", "task", "claude", 100, Decimal("1.00"), LIMITS)
    before = usage_row(database, "P", "task")
    rejected = [
        (10, Decimal("-0.01")),
        (10, Decimal("NaN")),
        (10, Decimal("Infinity")),
        (10, Decimal("-Infinity")),
        (10, float("nan")),
        (10, float("inf")),
        (-1, Decimal("0.10")),
        (-1, None),
    ]
    for tokens, usd in rejected:
        with pytest.raises(ValueError):
            ledger.observe("P", "task", tokens, usd)
        assert usage_row(database, "P", "task") == before
    with pytest.raises(ValueError, match="outstanding"):
        ledger.observe("P", "missing", 10, Decimal("0.10"))
    with pytest.raises(ValueError, match="outstanding"):
        ledger.observe("P", "missing", 10, None)
    assert reservation_count(database) == 1

    repo = make_repo(tmp_path / "repo")
    live_db = tmp_path / "live.db"
    reserve(live_db, "PLAN", "DEV-128")
    reserve(live_db, "PLAN", "DEV-129")
    engine = LifecycleEngine(repo, live_db)
    run_agent(engine, "DEV-128", claude_usage(4, 1, "-1"))
    run_agent(engine, "DEV-129", claude_usage(4, 1, "NaN"))
    assert usage_row(live_db, "PLAN", "DEV-128") == (None, None, "reserved", "null")
    assert usage_row(live_db, "PLAN", "DEV-129") == (None, None, "reserved", "null")

    imported_root = tmp_path / "import"
    imported_root.mkdir()
    monkeypatch.chdir(imported_root)
    imported = imported_root / ".codehelm" / "state.db"
    reserve(imported, "PLAN", "PLAN-S1-A1")
    usage_file = tmp_path / "import" / "usage.json"
    args = ["usage-import", "PLAN-S1-A1", "--provider", "claude",
            "--usage-file", str(usage_file), "--prefix", "PLAN", "--execute"]
    for cost in ("-1", "NaN", "Infinity"):
        usage_file.write_text(claude_usage(1, 1, cost), encoding="utf-8")
        result = runner.invoke(app, args)
        assert result.exit_code == 1
        assert usage_row(imported, "PLAN", "PLAN-S1-A1") == (None, None, "reserved", "null")
