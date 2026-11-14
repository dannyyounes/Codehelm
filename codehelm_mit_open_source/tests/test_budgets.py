from decimal import Decimal
import pytest
from codehelm.orchestrator.budgets import BudgetLedger, Limits


def test_reserve_reconcile_and_report(tmp_path):
    ledger = BudgetLedger(tmp_path / "db.sqlite")
    limit = Limits(1000, Decimal("2.00"))
    ledger.reserve("P", "P-S1-A1", "claude", 600, Decimal("1.25"), limit)
    assert ledger.report("P")["unreconciled"] == 1
    with pytest.raises(ValueError, match="exhausted"):
        ledger.reserve("P", "P-S2-A1", "codex", 500, Decimal("1"), limit)
    ledger.reconcile("P", "P-S1-A1", 250, Decimal("0.50"))
    ledger.reserve("P", "P-S2-A1", "codex", 500, Decimal("1"), limit)
    assert ledger.report("P")["accounted_tokens"] == 750
    assert ledger.report("P")["accounted_usd"] == "1.50"


def test_duplicate_reservation_and_double_reconciliation(tmp_path):
    ledger = BudgetLedger(tmp_path / "db.sqlite")
    args = ("P", "task", "claude", 100, Decimal("0.25"), Limits(1000, Decimal("2")))
    ledger.reserve(*args)
    with pytest.raises(ValueError, match="already"):
        ledger.reserve(*args)
    ledger.reconcile("P", "task", 90, Decimal("0.20"))
    with pytest.raises(ValueError, match="outstanding"):
        ledger.reconcile("P", "task", 90, Decimal("0.20"))


def test_negative_and_nonfinite_rejected(tmp_path):
    ledger = BudgetLedger(tmp_path / "db.sqlite")
    with pytest.raises(ValueError):
        Limits(100, Decimal("NaN"))
    with pytest.raises(ValueError):
        ledger.reserve("P", "task", "claude", -1, Decimal("1"), Limits(100, Decimal("2")))
