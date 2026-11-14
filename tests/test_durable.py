import json
from datetime import datetime, timedelta, timezone
import sqlite3
import pytest
from codehelm.planning.planner import parse_plan
from codehelm.orchestrator.durable import DurableScheduler, DispatchBudget


@pytest.fixture
def scheduler(tmp_path):
    plan = parse_plan(json.dumps({
        "epic_title":"Feature", "epic_description":"Build it",
        "stories":[
            {"id":"S1","title":"First","description":"First","acceptance_criteria":["Works"],"dependencies":[]},
            {"id":"S2","title":"Second","description":"Second","acceptance_criteria":["Works"],"dependencies":["S1"]}
        ]}))
    return DurableScheduler(tmp_path / "state.db", plan, "DEV")


def test_claim_blocks_dependencies_and_duplicate_claims(scheduler):
    assert scheduler.claim(DispatchBudget()) == ["S1"]
    assert scheduler.claim(DispatchBudget()) == []
    scheduler.finish("S1", "awaiting_validation")
    assert scheduler.claim(DispatchBudget()) == []


def test_retry_is_explicit_and_bounded(scheduler):
    scheduler.claim(DispatchBudget())
    scheduler.finish("S1", "failed")
    scheduler.retry("S1", DispatchBudget())
    assert scheduler.claim(DispatchBudget()) == ["S1"]
    scheduler.finish("S1", "failed")
    with pytest.raises(ValueError, match="limit"):
        scheduler.retry("S1", DispatchBudget())


def test_expired_claim_is_not_relaunched(scheduler):
    scheduler.claim(DispatchBudget())
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with sqlite3.connect(scheduler.store.database) as db:
        db.execute("UPDATE dispatches SET claimed_at=? WHERE story_id='S1'", (old,))
    assert scheduler.recover_expired(DispatchBudget()) == 1
    assert scheduler.claim(DispatchBudget()) == []
    assert scheduler.snapshot()["S1"][0] == "interrupted"


def test_global_attempt_budget(scheduler):
    assert scheduler.claim(DispatchBudget(max_total_attempts=1)) == ["S1"]
    scheduler.finish("S1", "failed")
    scheduler.retry("S1", DispatchBudget())
    assert scheduler.claim(DispatchBudget(max_total_attempts=1)) == []


def test_completion_requires_pr(scheduler):
    scheduler.claim(DispatchBudget())
    scheduler.finish("S1", "awaiting_validation")
    with pytest.raises(ValueError, match="draft PR"):
        scheduler.complete("S1", human_verified_merge=True)


def test_budget_validation():
    with pytest.raises(ValueError):
        DispatchBudget(max_active=9)


def test_manual_completion_requires_human_verification(scheduler):
    scheduler.claim(DispatchBudget())
    scheduler.finish("S1", "awaiting_validation")
    with pytest.raises(ValueError, match="Human verification"):
        scheduler.complete("S1")
