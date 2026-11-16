"""An omitted --max-attempts uses the quality policy attempt cap."""
import json
from pathlib import Path

import pytest

from codehelm.orchestrator.durable import DispatchBudget, DurableScheduler, resolve_max_attempts
from codehelm.planning.planner import parse_plan
from codehelm.quality.contract import QualityPolicy


def plan_json(*dependencies):
    stories = []
    for index, deps in enumerate(dependencies, start=1):
        stories.append({
            "id": f"S{index}", "title": f"Story {index}", "description": "Build it",
            "acceptance_criteria": ["Works"], "dependencies": list(deps),
        })
    return json.dumps({
        "epic_title": "Feature", "epic_description": "Build it", "stories": stories,
    })


def scheduler_for(database: Path, raw: str | None = None, prefix: str = "DEV") -> DurableScheduler:
    return DurableScheduler(database, parse_plan(raw or plan_json((), ("S1",))), prefix)


def test_requested_attempts_override_policy_max_retries():
    policy = QualityPolicy(max_retries=1)
    assert resolve_max_attempts(policy, 3) == 3


def test_present_policy_supplies_max_retries_when_unrequested():
    policy = QualityPolicy(present=True, max_retries=1)
    assert resolve_max_attempts(policy, None) == 1


def test_absent_policy_defaults_to_two_attempts():
    assert resolve_max_attempts(QualityPolicy.absent(), None) == 2


def test_retry_refuses_a_second_retry_when_max_attempts_is_one(tmp_path):
    scheduler = scheduler_for(tmp_path / "state.db")
    assert scheduler.claim(DispatchBudget(max_attempts=1)) == ["S1"]
    scheduler.finish("S1", "failed")
    with pytest.raises(ValueError, match="Per-story attempt limit reached"):
        scheduler.retry("S1", DispatchBudget(max_attempts=1))
    assert scheduler.snapshot()["S1"] == ("failed", 1)
