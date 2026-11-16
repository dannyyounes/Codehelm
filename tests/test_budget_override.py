"""One-shot budget overrides admit one exact additional-charge reservation."""
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.orchestrator.budgets import Limits
from codehelm.orchestrator.durable import DispatchBudget, DurableScheduler
from codehelm.orchestrator.monthly_budget import MonthlyBudget, MonthlyPolicy, parse_override_amount
from codehelm.planning.planner import parse_plan


def _overrides(path):
    with sqlite3.connect(path) as db:
        return db.execute(
            """SELECT task_id, epic, month, amount, approver, reason, used_at
               FROM budget_overrides ORDER BY id""").fetchall()


def test_reserve_over_the_cap_still_pauses_without_an_override(tmp_path):
    ledger = MonthlyBudget(tmp_path / "state.db")
    with pytest.raises(ValueError, match="Budget approval required; new task paused"):
        ledger.reserve("over-epic", "E1", Decimal("5.01"), month="2026-11")
    for epic, task in (("E1", "a"), ("E2", "b"), ("E3", "c"), ("E4", "d")):
        ledger.reserve(task, epic, Decimal("5"), month="2026-11")
    with pytest.raises(ValueError, match="Budget approval required; new task paused"):
        ledger.reserve("over-month", "E5", Decimal("0.01"), month="2026-11")
    assert _overrides(tmp_path / "state.db") == []


def test_matching_override_authorizes_one_reserve_then_pauses(tmp_path):
    ledger = MonthlyBudget(tmp_path / "state.db")
    ledger.record_override(
        "task-1", "E1", Decimal("6"), "dannyyounes", "incident", month="2026-11")
    with patch("codehelm.integrations.github_pr.github_user_login",
               side_effect=AssertionError("reserve must not call GitHub")):
        status = ledger.reserve("task-1", "E1", Decimal("6"), month="2026-11")
    assert status["accounted_aud"] == "6"
    assert _overrides(tmp_path / "state.db")[0][6] is not None
    with pytest.raises(ValueError, match="Budget approval required; new task paused"):
        ledger.reserve("task-1", "E1", Decimal("6"), month="2026-11")
    with pytest.raises(ValueError, match="Budget approval required; new task paused"):
        ledger.reserve("task-2", "E1", Decimal("6"), month="2026-11")


def test_mismatched_amount_pauses_and_does_not_consume_the_override(tmp_path):
    path = tmp_path / "state.db"
    ledger = MonthlyBudget(path)
    ledger.record_override(
        "task-1", "E1", Decimal("6.00"), "dannyyounes", "incident", month="2026-11")
    with pytest.raises(ValueError, match="Budget approval required; new task paused"):
        ledger.reserve("task-1", "E1", Decimal("6.01"), month="2026-11")
    assert _overrides(path)[0][6] is None
    with pytest.raises(ValueError, match="Budget approval required; new task paused"):
        ledger.reserve("task-1", "E2", Decimal("6.00"), month="2026-11")
    with pytest.raises(ValueError, match="Budget approval required; new task paused"):
        ledger.reserve("other", "E1", Decimal("6.00"), month="2026-11")
    with pytest.raises(ValueError, match="Budget approval required; new task paused"):
        ledger.reserve("task-1", "E1", Decimal("6.00"), month="2026-12")
    assert _overrides(path)[0][6] is None
    ledger.reserve("task-1", "E1", Decimal("6"), month="2026-11")
    assert _overrides(path)[0][6] is not None


def test_second_unused_override_for_the_same_task_is_rejected(tmp_path):
    path = tmp_path / "state.db"
    ledger = MonthlyBudget(path)
    ledger.record_override(
        "task-1", "E1", Decimal("6"), "dannyyounes", "first reason", month="2026-11")
    with pytest.raises(ValueError, match="already exists"):
        ledger.record_override(
            "task-1", "E9", Decimal("9"), "other", "second reason", month="2026-12")
    assert _overrides(path) == [
        ("task-1", "E1", "2026-11", "6", "dannyyounes", "first reason", None)]


def test_extra_decimal_places_can_match_and_subscription_does_not_consume(tmp_path):
    path = tmp_path / "state.db"
    ledger = MonthlyBudget(path)
    ledger.record_override(
        "precise", "E1", Decimal("5.125"), "dannyyounes", "precision", month="2026-11")
    ledger.reserve("sub", "E1", Decimal("100"), month="2026-11", charge_type="subscription")
    assert _overrides(path)[0][6] is None
    ledger.reserve("precise", "E1", Decimal("5.125"), month="2026-11")
    assert _overrides(path)[0][6] is not None


def test_scheduler_claims_one_story_past_the_epic_cap_when_the_override_matches(tmp_path):
    plan = parse_plan(json.dumps({
        "epic_title": "Epic", "epic_description": "Description",
        "stories": [
            {"id": f"S{i}", "title": f"Story {i}", "description": "Implement",
             "acceptance_criteria": ["Pass"], "dependencies": []}
            for i in range(1, 4)
        ],
    }))
    db = tmp_path / "state.db"
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    MonthlyBudget(db).record_override(
        "E1-S3-A1", "E1", Decimal("2.50"), "dannyyounes", "incident", month=month)
    scheduler = DurableScheduler(db, plan, "E1")
    chosen = scheduler.claim_budgeted(
        DispatchBudget(max_active=3), "claude", 100, Decimal("0.5"),
        Limits(1000, Decimal("10")), MonthlyPolicy(), Decimal("2.50"), month=month)
    assert chosen == ["S1", "S2", "S3"]
    with sqlite3.connect(db) as connection:
        used = connection.execute(
            "SELECT used_at FROM budget_overrides WHERE task_id='E1-S3-A1'").fetchone()[0]
    assert used is not None
    again = scheduler.claim_budgeted(
        DispatchBudget(max_active=3), "claude", 100, Decimal("0.5"),
        Limits(1000, Decimal("10")), MonthlyPolicy(), Decimal("2.50"), month=month)
    assert again == []


def test_override_amount_rejects_non_positive_and_non_numeric():
    assert parse_override_amount(" 1.234 ") == Decimal("1.234")
    for bad in ("0", "0.00", "-1", "-0.01", "nope", "NaN", "Infinity", "", "  "):
        with pytest.raises(ValueError, match="Invalid budget override amount"):
            parse_override_amount(bad)


def test_budget_approve_dry_run_does_not_call_github(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("codehelm.cli.main.github_user_login",
               side_effect=AssertionError("github")) as lookup:
        result = CliRunner().invoke(app, [
            "budget-approve", "TASK-1", "--epic", "EPIC", "--amount", "2.50",
            "--approver", "dannyyounes", "--reason", "incident",
        ])
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert "GitHub is not called" in result.output
    assert not lookup.called
    assert not (tmp_path / ".codehelm" / "state.db").exists()


def test_budget_approve_mismatch_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("codehelm.cli.main.github_user_login", return_value="dannyyounes") as lookup:
        result = CliRunner().invoke(app, [
            "budget-approve", "TASK-1", "--epic", "EPIC", "--amount", "2.50",
            "--approver", "someone-else", "--reason", "incident", "--execute",
        ])
    assert result.exit_code == 1
    assert "does not match" in result.output
    assert lookup.called
    assert not (tmp_path / ".codehelm" / "state.db").exists()


def test_budget_approve_empty_reason_and_bad_amount_skip_github(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    with patch("codehelm.cli.main.github_user_login",
               side_effect=AssertionError("github")) as lookup:
        empty = runner.invoke(app, [
            "budget-approve", "TASK-1", "--epic", "EPIC", "--amount", "2.50",
            "--approver", "dannyyounes", "--reason", "   ", "--execute",
        ])
        zero = runner.invoke(app, [
            "budget-approve", "TASK-1", "--epic", "EPIC", "--amount", "0",
            "--approver", "dannyyounes", "--reason", "incident", "--execute",
        ])
        negative = runner.invoke(app, [
            "budget-approve", "TASK-1", "--epic", "EPIC", "--amount", "-1",
            "--approver", "dannyyounes", "--reason", "incident", "--execute",
        ])
        text = runner.invoke(app, [
            "budget-approve", "TASK-1", "--epic", "EPIC", "--amount", "nope",
            "--approver", "dannyyounes", "--reason", "incident", "--execute",
        ])
    assert empty.exit_code == 1 and "reason" in empty.output
    assert zero.exit_code == 1 and negative.exit_code == 1 and text.exit_code == 1
    assert "Invalid budget override amount" in zero.output
    assert "Invalid budget override amount" in text.output
    assert not lookup.called
    assert not (tmp_path / ".codehelm" / "state.db").exists()


def test_budget_approve_match_stores_the_login(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("codehelm.cli.main.github_user_login", return_value="dannyyounes") as lookup:
        result = CliRunner().invoke(app, [
            "budget-approve", "TASK-1", "--epic", "EPIC-9", "--amount", "2.50",
            "--approver", " dannyyounes ", "--reason", " incident bridge ", "--execute",
        ])
    assert result.exit_code == 0, result.output
    assert lookup.called
    row = _overrides(tmp_path / ".codehelm" / "state.db")[0]
    assert row[0] == "TASK-1"
    assert row[1] == "EPIC-9"
    assert row[2] == datetime.now(timezone.utc).strftime("%Y-%m")
    assert row[3] == "2.50"
    assert row[4] == "dannyyounes"
    assert row[5] == "incident bridge"
    assert row[6] is None
