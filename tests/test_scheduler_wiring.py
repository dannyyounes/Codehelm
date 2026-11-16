"""Opt-in scheduler wiring: merge, published Jira keys, and reserve-usd conversion."""

import json
import sqlite3
import subprocess
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.orchestrator.durable import DurableScheduler
from codehelm.orchestrator.fx import FRANKFURTER_SOURCE
from codehelm.orchestrator.lifecycle import TaskStore
from codehelm.planning.planner import load_plan
from codehelm.quality.contract import ContractDecision

URL = "https://github.com/dannyyounes/Codehelm/pull/1"
HEAD = "a" * 40
runner = CliRunner()


def _plan(path: Path) -> None:
    path.write_text(json.dumps({
        "epic_title": "Epic", "epic_description": "Description",
        "stories": [{
            "id": "S1", "title": "Story", "description": "Implement",
            "acceptance_criteria": ["Pass"], "dependencies": [],
        }],
    }), encoding="utf-8")


def _repo(path: Path) -> Path:
    path.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)

    git("init")
    git("config", "user.email", "test@example.org")
    git("config", "user.name", "Test")
    (path / "README.md").write_text("Initial\n")
    git("add", ".")
    git("commit", "-m", "initial")
    return path


def _ready(tmp_path: Path, plan: Path, *, auto_merge: bool) -> None:
    state = tmp_path / ".codehelm"
    state.mkdir(exist_ok=True)
    (state / "quality.yaml").write_text(
        "schema_version: 1\n"
        "auto_merge: " + ("true" if auto_merge else "false") + "\n"
        "require_tests: false\n"
        "require_security: false\n"
        "require_review: false\n"
        "require_architecture: false\n"
        "require_docs: false\n"
        "require_acceptance_criteria: false\n",
        encoding="utf-8",
    )
    scheduler = DurableScheduler(state / "state.db", load_plan(plan), "E1")
    with sqlite3.connect(scheduler.store.database) as db:
        db.execute("UPDATE dispatches SET state='ready_for_pr', attempts=1")
    TaskStore(state / "state.db").transition(
        "E1-S1-A1", "pr_created", error=URL, workspace=str(tmp_path), provider="claude")


def _durable(plan: Path, repo: Path, *extra: str):
    return [
        "schedule-durable", "--plan-file", str(plan), "--prefix", "E1", "--repo", str(repo),
        "--reserve-tokens", "100", "--reserve-usd", "0.50",
        "--limit-tokens", "1000", "--limit-usd", "10", *extra,
    ]


def test_durable_dry_run_merges_only_when_auto_merge_is_on(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.json"
    _plan(plan)
    _ready(tmp_path, plan, auto_merge=False)
    ready = ContractDecision("ready_for_pr", (), True)
    with patch("codehelm.orchestrator.durable.assess", return_value=ready), \
         patch("codehelm.orchestrator.durable.github_pull_request_green") as checks, \
         patch("codehelm.orchestrator.durable.github_merge_pull_request") as merge:
        off = runner.invoke(app, _durable(plan, tmp_path))
    assert off.exit_code == 0, off.output
    assert "Would merge" not in off.output
    assert not checks.called and not merge.called
    (tmp_path / ".codehelm" / "quality.yaml").write_text(
        (tmp_path / ".codehelm" / "quality.yaml").read_text().replace(
            "auto_merge: false", "auto_merge: true"),
        encoding="utf-8",
    )
    with patch("codehelm.orchestrator.durable.assess", return_value=ready), \
         patch("codehelm.orchestrator.durable.github_pull_request_green") as checks, \
         patch("codehelm.orchestrator.durable.github_merge_pull_request") as merge:
        allowed = runner.invoke(app, _durable(plan, tmp_path))
    assert allowed.exit_code == 0, allowed.output
    assert "Would merge" in allowed.output
    assert "Not merged" in allowed.output
    assert not checks.called and not merge.called


def test_durable_execute_merges_and_leaves_the_story_incomplete(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = _repo(tmp_path / "repo")
    plan = tmp_path / "plan.json"
    _plan(plan)
    _ready(tmp_path, plan, auto_merge=True)
    ready = ContractDecision("ready_for_pr", (), True)
    store = TaskStore(tmp_path / ".codehelm" / "state.db")
    with patch("codehelm.orchestrator.durable.assess", return_value=ready), \
         patch("codehelm.orchestrator.durable.github_pull_request_green", return_value=HEAD) as checks, \
         patch("codehelm.orchestrator.durable.github_merge_pull_request") as merge:
        merged = runner.invoke(app, _durable(plan, repo, "--execute"))
    assert merged.exit_code == 0, merged.output
    assert "not completed" in merged.output
    checks.assert_called_once_with(URL)
    merge.assert_called_once_with(URL, HEAD)
    assert store.get("E1-S1-A1").status == "pr_created"
    with sqlite3.connect(store.database) as db:
        state = db.execute("SELECT state FROM dispatches").fetchone()[0]
    assert state == "ready_for_pr"
    with patch("codehelm.orchestrator.durable.assess", return_value=ready), \
         patch("codehelm.orchestrator.durable.github_pull_request_green",
               side_effect=ValueError("CI is not green")), \
         patch("codehelm.orchestrator.durable.github_merge_pull_request") as merge:
        red = runner.invoke(app, _durable(plan, repo, "--execute"))
    assert red.exit_code == 1
    assert "not green" in red.output
    assert not merge.called
    assert store.get("E1-S1-A1").status == "pr_created"


def test_plain_schedule_does_not_merge(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.json"
    repo = _repo(tmp_path / "repo")
    _plan(plan)
    with patch("codehelm.orchestrator.durable.github_merge_pull_request") as merge:
        result = runner.invoke(app, ["schedule", "--plan-file", str(plan), "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert not merge.called


def test_publish_writes_story_keys_and_refuses_a_second_time(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.json"
    _plan(plan)
    dry = runner.invoke(app, ["plan-publish", "--project", "DEV", "--plan-file", str(plan)])
    assert dry.exit_code == 0
    assert load_plan(plan).stories[0].jira_key == ""

    class FakeJira:
        def __init__(self):
            self.calls = 0

        def create_issue(self, project, title, kind, description="", parent=None):
            self.calls += 1
            return {"key": f"DEV-{self.calls}"}

    with patch("codehelm.cli.main.JiraClient.from_env", return_value=FakeJira()):
        published = runner.invoke(app, [
            "plan-publish", "--project", "DEV", "--plan-file", str(plan), "--execute"])
    assert published.exit_code == 0, published.output
    stored = load_plan(plan)
    assert stored.stories[0].jira_key == "DEV-2"
    assert "epic" not in json.loads(plan.read_text())
    with patch("codehelm.cli.main.JiraClient.from_env") as client:
        again = runner.invoke(app, [
            "plan-publish", "--project", "DEV", "--plan-file", str(plan), "--execute"])
    assert again.exit_code == 1
    assert "already records" in again.output
    assert not client.called
    assert load_plan(plan).stories[0].jira_key == "DEV-2"


def test_publish_does_not_store_a_malformed_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.json"
    _plan(plan)
    original = plan.read_text()

    class FakeJira:
        def create_issue(self, project, title, kind, description="", parent=None):
            return {"key": "not-a-key"}

    with patch("codehelm.cli.main.JiraClient.from_env", return_value=FakeJira()):
        result = runner.invoke(app, [
            "plan-publish", "--project", "DEV", "--plan-file", str(plan), "--execute"])
    assert result.exit_code == 1
    assert "usable issue key" in result.output
    assert plan.read_text() == original


def test_convert_reserve_usd_is_opt_in_and_fail_closed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = _repo(tmp_path / "repo")
    plan = tmp_path / "plan.json"
    _plan(plan)
    base = _durable(plan, repo)
    with patch("codehelm.cli.main.fetch_aud_per_usd") as fetch:
        missing = runner.invoke(app, base + ["--fetch-aud-rate", "--convert-reserve-usd"])
    assert missing.exit_code == 0, missing.output
    assert "from --reserve-usd" in missing.output
    assert "0.50" in missing.output
    assert "Not recorded" in missing.output
    fetch.assert_not_called()
    with patch("codehelm.cli.main.fetch_aud_per_usd") as fetch:
        bare = runner.invoke(app, base + ["--convert-reserve-usd"])
        both = runner.invoke(app, base + [
            "--fetch-aud-rate", "--convert-reserve-usd", "--additional-usd", "1.50"])
        still = runner.invoke(app, base + ["--fetch-aud-rate"])
    assert bare.exit_code == 1 and "requires --fetch-aud-rate" in bare.output
    assert both.exit_code == 1 and "cannot be combined" in both.output
    assert still.exit_code == 1 and "requires a USD amount" in still.output
    assert not fetch.called
    seen = {}

    def claim(self, budget, provider, tokens, usd, limits, monthly, aud, **kwargs):
        seen["usd"] = usd
        seen["aud"] = aud
        seen["rate"] = kwargs.get("aud_rate")
        seen["as_of"] = kwargs.get("aud_rate_as_of")
        return []

    with patch("codehelm.cli.main.fetch_aud_per_usd",
               return_value=(Decimal("1.54"), FRANKFURTER_SOURCE, "2026-11-16")), \
         patch("codehelm.orchestrator.durable.DurableScheduler.claim_budgeted", claim):
        executed = runner.invoke(app, base + ["--execute", "--fetch-aud-rate", "--convert-reserve-usd"])
    assert executed.exit_code == 0, executed.output
    assert "0.7700" in executed.output
    assert seen["usd"] == Decimal("0.50")
    assert seen["aud"] == Decimal("0.7700")
    assert seen["rate"] == Decimal("1.54")
    assert seen["as_of"] == "2026-11-16"
