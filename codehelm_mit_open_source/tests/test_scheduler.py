import json
from dataclasses import dataclass
from unittest.mock import Mock
import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.planning.planner import parse_plan
from codehelm.orchestrator.scheduler import ready_stories, dispatch_batch


@pytest.fixture
def plan():
    return parse_plan(json.dumps({
        "epic_title": "Feature", "epic_description": "Feature description",
        "stories": [
            {"id": "S1", "title": "API", "description": "Build API",
             "acceptance_criteria": ["Tests pass"], "dependencies": []},
            {"id": "S2", "title": "UI", "description": "Build UI",
             "acceptance_criteria": ["Works"], "dependencies": ["S1"]},
            {"id": "S3", "title": "Docs", "description": "Add docs",
             "acceptance_criteria": ["Documented"], "dependencies": []},
        ]
    }))


def test_ready_respects_dependencies(plan):
    assert [s.id for s in ready_stories(plan, set(), set())] == ["S1", "S3"]
    assert [s.id for s in ready_stories(plan, {"S1"}, {"S3"})] == ["S2"]


def test_unknown_completed_fails(plan):
    with pytest.raises(ValueError, match="Unknown"):
        ready_stories(plan, {"S99"}, set())


def test_batch_is_bounded_and_provider_routed(plan):
    runner = Mock(return_value=Mock(exit_code=0, error=""))
    result = dispatch_batch(plan, set(), set(), ["claude", "codex"], "DEV", 2, runner)
    assert [r.story_id for r in result] == ["S1", "S3"]
    assert [r.provider for r in result] == ["claude", "codex"]
    assert all(r.status == "awaiting_validation" for r in result)
    assert runner.call_count == 2


def test_no_automatic_dependency_unlock(plan):
    runner = Mock(return_value=Mock(exit_code=0, error=""))
    result = dispatch_batch(plan, set(), set(), ["claude"], "DEV", 8, runner)
    assert "S2" not in [r.story_id for r in result]


def test_failed_agent_reported(plan):
    runner = Mock(return_value=Mock(exit_code=1, error="failure"))
    result = dispatch_batch(plan, set(), set(), ["claude"], "DEV", 1, runner)
    assert result[0].status == "failed"


def test_invalid_parallelism(plan):
    with pytest.raises(ValueError, match="max_parallel"):
        dispatch_batch(plan, set(), set(), ["claude"], "DEV", 9, Mock())


def test_schedule_dry_run(tmp_path, plan, monkeypatch):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    file = tmp_path / "plan.json"
    file.write_text(json.dumps(plan.to_dict()))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["schedule", "--plan-file", str(file),
                                      "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "S1, S3" in result.output
    assert "Dry run" in result.output
