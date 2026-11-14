import json
from decimal import Decimal
import pytest
from codehelm.orchestrator.monthly_budget import MonthlyBudget, MonthlyPolicy
from codehelm.orchestrator.budgets import Limits
from codehelm.orchestrator.durable import DurableScheduler, DispatchBudget
from codehelm.planning.planner import parse_plan


def test_monthly_thresholds_and_epic_cap(tmp_path):
    ledger = MonthlyBudget(tmp_path / "state.db")
    assert ledger.reserve("a", "E1", Decimal("10"), charge_type="subscription")["level"] == "normal"
    assert ledger.reserve("b", "E1", Decimal("5"))["level"] == "normal"
    with pytest.raises(ValueError, match="approval"):
        ledger.reserve("c", "E1", Decimal("0.01"))
    ledger.reserve("d", "E2", Decimal("5"))
    assert ledger.status()["level"] == "warning"
    ledger.reserve("e", "E3", Decimal("5"))
    assert ledger.status()["level"] == "critical"
    ledger.reserve("f", "E4", Decimal("5"))
    assert ledger.status()["level"] == "paused"
    with pytest.raises(ValueError, match="approval"):
        ledger.reserve("g", "E5", Decimal("1"))


def test_atomic_monthly_scheduler_gate(tmp_path):
    plan = parse_plan(json.dumps({"epic_title":"Epic","epic_description":"Description",
        "stories":[{"id":f"S{i}","title":f"Story {i}","description":"Implement",
        "acceptance_criteria":["Pass"],"dependencies":[]} for i in range(1,4)]}))
    db = tmp_path / "state.db"
    scheduler = DurableScheduler(db,plan,"E1")
    chosen = scheduler.claim_budgeted(DispatchBudget(max_active=3),"claude",100,
        Decimal("0.5"),Limits(1000,Decimal("10")),MonthlyPolicy(),
        Decimal("2.50"))
    assert chosen == ["S1","S2"]
    assert scheduler.snapshot()["S3"] == ("pending",0)
    assert MonthlyBudget(db).status()["accounted_aud"] == "5.00"
