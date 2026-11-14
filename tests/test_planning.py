import json
from unittest.mock import patch
import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.planning.planner import (
    PlanningError, generate_plan, load_plan, parse_plan, publish_plan_to_jira, save_plan
)
from codehelm.providers.adapters import AgentResult


def sample():
    return {"epic_title": "Build login", "epic_description": "Secure sign-in",
            "stories": [
                {"id": "S1", "title": "Backend", "description": "Add endpoint",
                 "acceptance_criteria": ["Reject invalid passwords"], "dependencies": []},
                {"id": "S2", "title": "Frontend", "description": "Add form",
                 "acceptance_criteria": ["Shows validation errors"], "dependencies": ["S1"]},
            ]}


def test_parse_and_save(tmp_path):
    plan = parse_plan(json.dumps(sample()))
    save_plan(plan, tmp_path / "plan.json")
    assert load_plan(tmp_path / "plan.json") == plan
    with pytest.raises(PlanningError, match="already exists"):
        save_plan(plan, tmp_path / "plan.json")


def test_cycle_rejected():
    data = sample()
    data["stories"][0]["dependencies"] = ["S2"]
    with pytest.raises(PlanningError, match="cycle"):
        parse_plan(json.dumps(data))


def test_unknown_dependency_rejected():
    data = sample()
    data["stories"][1]["dependencies"] = ["S99"]
    with pytest.raises(PlanningError, match="Unknown dependency"):
        parse_plan(json.dumps(data))


def test_duplicate_story_rejected():
    data = sample()
    data["stories"][1]["id"] = "S1"
    with pytest.raises(PlanningError, match="unique"):
        parse_plan(json.dumps(data))


def test_non_json_rejected():
    with pytest.raises(PlanningError, match="JSON"):
        parse_plan("```json\n{}\n```")


def test_generate_uses_provider(tmp_path):
    with patch("codehelm.planning.planner.CLIProvider.execute",
               return_value=AgentResult("claude", 0, json.dumps(sample()), "")):
        assert len(generate_plan("Build login", "claude", tmp_path).stories) == 2


def test_publish_dependency_order():
    class FakeJira:
        def __init__(self):
            self.calls = []
        def create_issue(self, project, title, kind, description="", parent=None):
            self.calls.append((title, kind, description, parent))
            return {"key": f"DEV-{len(self.calls)}"}
    fake = FakeJira()
    result = publish_plan_to_jira(parse_plan(json.dumps(sample())), "DEV", fake)
    assert result == {"epic": "DEV-1", "S1": "DEV-2", "S2": "DEV-3"}
    assert "DEV-2" in fake.calls[2][2]
    assert fake.calls[2][3] == "DEV-1"


def test_plan_cli_dry_run():
    result = CliRunner().invoke(app, ["plan", "--goal", "Build login"])
    assert result.exit_code == 0
    assert "Dry run" in result.output


def test_publish_cli_dry_run():
    result = CliRunner().invoke(app, ["plan-publish", "--project", "DEV"])
    assert result.exit_code == 0
    assert "Dry run" in result.output
