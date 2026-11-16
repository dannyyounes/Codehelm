"""AUD conversion uses only an operator-supplied rate and source."""
import json
import sqlite3
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.orchestrator.budgets import Limits
from codehelm.orchestrator.durable import DispatchBudget, DurableScheduler
from codehelm.orchestrator.fx import resolve_additional_aud
from codehelm.orchestrator.monthly_budget import MonthlyPolicy
from codehelm.planning.planner import parse_plan

runner = CliRunner()


def test_direct_aud_amount_does_not_invent_a_rate():
    aud, rate, source = resolve_additional_aud("2.50", None, None, "")
    assert aud == Decimal("2.50")
    assert rate is None and source is None


def test_conversion_multiplies_the_supplied_rate():
    aud, rate, source = resolve_additional_aud("0", "1.50", "1.54", "RBA noon 2026-11-17")
    assert aud == Decimal("1.50") * Decimal("1.54")
    assert rate == Decimal("1.54")
    assert source == "RBA noon 2026-11-17"


@pytest.mark.parametrize("usd,rate,source", [
    (None, "1.54", "desk"),
    ("1.50", None, "desk"),
    ("1.50", "1.54", ""),
    ("0", "1.54", "desk"),
    ("1.50", "0", "desk"),
    ("1.50", "-1", "desk"),
    ("1.50", "NaN", "desk"),
    ("-1", "1.54", "desk"),
])
def test_incomplete_or_nonpositive_conversion_fails(usd, rate, source):
    with pytest.raises(ValueError):
        resolve_additional_aud("0", usd, rate, source)


def test_conversion_cannot_be_combined_with_a_direct_aud_amount():
    with pytest.raises(ValueError, match="not both"):
        resolve_additional_aud("2.50", "1.50", "1.54", "desk")


def test_quote_prints_the_product_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, [
        "aud-quote", "1.50", "--aud-per-usd", "1.54", "--rate-source", "desk sheet"])
    assert result.exit_code == 0, result.output
    assert "2.3100 AUD" in result.output
    assert "Not recorded" in result.output
    assert "desk sheet" in result.output
    assert not (tmp_path / ".codehelm" / "state.db").exists()
    missing = runner.invoke(app, ["aud-quote", "1.50", "--aud-per-usd", "1.54", "--rate-source", ""])
    assert missing.exit_code == 1


def test_claim_stores_the_rate_and_source(tmp_path):
    plan = parse_plan(json.dumps({
        "epic_title": "Epic", "epic_description": "Description",
        "stories": [{
            "id": "S1", "title": "Story", "description": "Implement",
            "acceptance_criteria": ["Pass"], "dependencies": [],
        }],
    }))
    database = tmp_path / "state.db"
    scheduler = DurableScheduler(database, plan, "E1")
    aud = Decimal("1.50") * Decimal("1.54")
    chosen = scheduler.claim_budgeted(
        DispatchBudget(), "claude", 100, Decimal("0.50"), Limits(1000, Decimal("10")),
        MonthlyPolicy(), aud, aud_rate=Decimal("1.54"), aud_rate_source="desk sheet")
    assert chosen == ["S1"]
    with sqlite3.connect(database) as db:
        row = db.execute(
            "SELECT estimated_aud, rate_aud_per_usd, rate_source FROM aud_reservations"
        ).fetchone()
    assert row == (str(aud), "1.54", "desk sheet")


def test_schedule_dry_run_quotes_without_a_reservation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "epic_title": "Epic", "epic_description": "Description",
        "stories": [{
            "id": "S1", "title": "Story", "description": "Implement",
            "acceptance_criteria": ["Pass"], "dependencies": [],
        }],
    }), encoding="utf-8")
    result = runner.invoke(app, [
        "schedule-durable", "--plan-file", str(plan), "--prefix", "E1",
        "--reserve-tokens", "100", "--reserve-usd", "0.50",
        "--limit-tokens", "1000", "--limit-usd", "10",
        "--additional-usd", "1.50", "--aud-per-usd", "1.54",
        "--aud-rate-source", "desk sheet",
    ])
    assert result.exit_code == 0, result.output
    assert "Would reserve 2.3100 AUD" in result.output
    assert "Not recorded" in result.output
    database = tmp_path / ".codehelm" / "state.db"
    with sqlite3.connect(database) as db:
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='aud_reservations'"
        ).fetchone()
    assert table is None
