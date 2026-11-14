import json
from decimal import Decimal
from codehelm.planning.planner import parse_plan
from codehelm.orchestrator.durable import DurableScheduler, DispatchBudget
from codehelm.orchestrator.budgets import BudgetLedger, Limits


def make_scheduler(tmp_path):
    plan = parse_plan(json.dumps({
        "epic_title":"Epic", "epic_description":"Build",
        "stories":[{"id": f"S{i}", "title":f"Story {i}", "description":"Build",
                    "acceptance_criteria":["Pass"],"dependencies":[]}
                   for i in range(1, 4)]}))
    return DurableScheduler(tmp_path / "state.db", plan, "PLAN")


def test_claim_and_reservation_are_atomic(tmp_path):
    scheduler = make_scheduler(tmp_path)
    chosen = scheduler.claim_budgeted(DispatchBudget(max_active=3), "claude", 100,
                                     Decimal("0.50"), Limits(250, Decimal("2")))
    assert chosen == ["S1", "S2"]
    report = BudgetLedger(scheduler.store.database).report("PLAN")
    assert report["entries"] == 2
    assert report["accounted_tokens"] == 200
    assert scheduler.claim_budgeted(DispatchBudget(max_active=3), "claude", 100,
                                    Decimal("0.50"), Limits(250, Decimal("2"))) == []


def test_heartbeat_only_live_claims(tmp_path):
    scheduler = make_scheduler(tmp_path)
    scheduler.claim_budgeted(DispatchBudget(), "claude", 100,
                             Decimal("0.50"), Limits(1000, Decimal("5")))
    assert scheduler.heartbeat("S1")
    scheduler.finish("S1", "awaiting_validation")
    assert not scheduler.heartbeat("S1")


def test_budget_exhaustion_does_not_claim(tmp_path):
    scheduler = make_scheduler(tmp_path)
    assert scheduler.claim_budgeted(DispatchBudget(), "claude", 100,
                                    Decimal("1"), Limits(50, Decimal("5"))) == []
    assert scheduler.snapshot()["S1"] == ("pending", 0)
