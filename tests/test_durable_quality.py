"""Durable wave runs quality gates from recorded process evidence."""
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.orchestrator.budgets import BudgetLedger, Limits
from codehelm.orchestrator.durable import DispatchBudget, DurableScheduler
from codehelm.orchestrator.lifecycle import ClaimLost, LifecycleEngine
from codehelm.planning.planner import parse_plan
from codehelm.providers.adapters import AgentResult


def git(repo, *args):
    import subprocess
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "test@example.org")
    git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "initial")
    return path


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


def write_policy(database: Path, extra: str = "") -> None:
    (database.parent / "quality.yaml").write_text(
        "schema_version: 1\n" + extra, encoding="utf-8")


def scheduler_for(database: Path, raw: str | None = None, prefix: str = "DEV") -> DurableScheduler:
    return DurableScheduler(database, parse_plan(raw or plan_json((), ("S1",))), prefix)


def admit(scheduler: DurableScheduler, budget: DispatchBudget | None = None,
          tokens: int = 100_000) -> list[str]:
    return scheduler.claim_budgeted(
        budget or DispatchBudget(), "claude", 100, Decimal("0.10"),
        Limits(tokens, Decimal("10")))


def write_change(cwd, risky: bool = False) -> None:
    src = Path(cwd) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    if risky:
        migrations = Path(cwd) / "migrations"
        migrations.mkdir(parents=True, exist_ok=True)
        (migrations / "001.sql").write_text("CREATE TABLE t(id INT);\n", encoding="utf-8")


def implement(prompt, cwd, timeout=600, risky: bool = False):
    write_change(cwd, risky=risky)
    return AgentResult("claude", 0, "done", "")


def document(prompt, cwd, timeout=600):
    readme = Path(cwd) / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "Documented.\n", encoding="utf-8")
    return AgentResult("claude", 0, "docs", "")


def approved():
    return AgentResult("codex", 0, "Checked the diff.\nAPPROVED", "")


def rejected():
    return AgentResult("codex", 0, "Boundary bug.\nCHANGES_REQUESTED", "")


PASS = ["python", "-c", "pass"]
FAIL_TESTS = ["python", "-c", "raise SystemExit(1)"]
FAIL_SECURITY = ["python", "-c", "raise SystemExit(2)"]


def carry(scheduler, engine, story_id="S1", *, test_command=None, security_command=None,
          reviewer="codex", heartbeat=None, risky=False):
    def code(prompt, cwd, timeout=600):
        return implement(prompt, cwd, timeout, risky=risky)

    with patch("codehelm.orchestrator.lifecycle.CLIProvider") as coder, \
         patch("codehelm.quality.gates.CLIProvider") as reviewer_provider, \
         patch("codehelm.agents.documentation.CLIProvider") as docs:
        coder.return_value.execute.side_effect = code
        reviewer_provider.return_value.execute.return_value = approved()
        docs.return_value.execute.side_effect = document
        status = scheduler.carry_story(
            story_id, engine, prompt=f"Implement {story_id}", provider="claude",
            test_command=list(test_command or PASS),
            security_command=list(security_command or PASS),
            reviewer=reviewer, docs_provider="claude", heartbeat=heartbeat)
    return status, coder, reviewer_provider, docs


def test_wave_reaches_ready_for_pr_without_inferring_acceptance(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    write_policy(database, "require_acceptance_criteria: false\n")
    scheduler = scheduler_for(database)
    engine = LifecycleEngine(repo, database)
    assert DispatchBudget().max_active == 2
    assert admit(scheduler) == ["S1"]
    status, _coder, reviewer, docs = carry(scheduler, engine)
    assert status == "ready_for_pr"
    assert scheduler.snapshot()["S1"] == ("ready_for_pr", 1)
    assert scheduler.snapshot()["S2"] == ("pending", 0)
    task_id = "DEV-S1-A1"
    assert engine.store.get(task_id).status == "ready_for_pr"
    evidence = engine.store.evidence_for(task_id, 1)
    tests = [row for row in evidence if row.gate == "tests"]
    security = [row for row in evidence if row.gate == "security"]
    assert len(tests) == 2 and all(row.exit_code == 0 and row.source == "command_gate" for row in tests)
    assert len(security) == 2 and all(row.exit_code == 0 for row in security)
    assert reviewer.return_value.execute.call_count == 3
    assert docs.return_value.execute.call_count == 1
    assert not [row for row in evidence if row.gate == "acceptance" and row.source == "human"]


def test_default_policy_blocks_until_acceptance_is_attested(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    write_policy(database)
    scheduler = scheduler_for(database)
    engine = LifecycleEngine(repo, database)
    assert admit(scheduler) == ["S1"]
    status, _coder, reviewer, _docs = carry(scheduler, engine)
    assert status == "blocked"
    latest = engine.store.latest_evaluation("DEV-S1-A1", 1)
    assert "unverified_acceptance_criteria" in latest["failures"]
    calls = reviewer.return_value.execute.call_count
    engine.store.record_evidence(
        "DEV-S1-A1", "acceptance", "human", passed=True, attempt=1, detail="Works")
    assert scheduler.apply_quality("S1") == "ready_for_pr"
    assert reviewer.return_value.execute.call_count == calls


def test_failing_tests_block_without_later_gates(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    write_policy(database, "require_acceptance_criteria: false\n")
    marker = tmp_path / "security-ran"
    security = ["python", "-c", f"open(r'{marker}', 'w').write('x')"]
    scheduler = scheduler_for(database)
    engine = LifecycleEngine(repo, database)
    assert admit(scheduler) == ["S1"]
    status, _coder, reviewer, docs = carry(scheduler, engine, test_command=FAIL_TESTS,
                                           security_command=security)
    assert status == "blocked"
    assert not marker.exists()
    assert reviewer.return_value.execute.call_count == 0
    assert docs.return_value.execute.call_count == 0
    tests = [row for row in engine.store.evidence_for("DEV-S1-A1", 1) if row.gate == "tests"]
    assert tests[-1].source == "command_gate" and tests[-1].exit_code == 1
    assert "tests_not_verified" in engine.store.latest_evaluation("DEV-S1-A1", 1)["failures"]


def test_failing_security_blocks_without_review(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    write_policy(database, "require_acceptance_criteria: false\n")
    scheduler = scheduler_for(database)
    engine = LifecycleEngine(repo, database)
    assert admit(scheduler) == ["S1"]
    status, _coder, reviewer, docs = carry(scheduler, engine, security_command=FAIL_SECURITY)
    assert status == "blocked"
    assert reviewer.return_value.execute.call_count == 0
    assert docs.return_value.execute.call_count == 0
    security = [row for row in engine.store.evidence_for("DEV-S1-A1", 1) if row.gate == "security"]
    assert security[-1].exit_code == 2 and not security[-1].passed
    assert "security_scan_not_verified" in engine.store.latest_evaluation("DEV-S1-A1", 1)["failures"]


def test_failing_review_blocks_without_docs(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    write_policy(database, "require_acceptance_criteria: false\n")
    scheduler = scheduler_for(database)
    engine = LifecycleEngine(repo, database)
    assert admit(scheduler) == ["S1"]
    with patch("codehelm.orchestrator.lifecycle.CLIProvider") as coder, \
         patch("codehelm.quality.gates.CLIProvider") as reviewer, \
         patch("codehelm.agents.documentation.CLIProvider") as docs:
        coder.return_value.execute.side_effect = implement
        reviewer.return_value.execute.return_value = rejected()
        docs.return_value.execute.side_effect = document
        status = scheduler.carry_story(
            "S1", engine, prompt="Implement S1", provider="claude",
            test_command=PASS, security_command=PASS, reviewer="codex")
    assert status == "blocked"
    assert reviewer.return_value.execute.call_count == 1
    assert docs.return_value.execute.call_count == 0
    review = [row for row in engine.store.evidence_for("DEV-S1-A1", 1) if row.gate == "review"][-1]
    assert review.source == "independent_review" and not review.passed
    assert "CHANGES_REQUESTED" in review.detail
    assert "independent_review_missing" in engine.store.latest_evaluation("DEV-S1-A1", 1)["failures"]


def test_high_risk_change_awaits_human_approval(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    write_policy(database, "require_acceptance_criteria: false\n")
    scheduler = scheduler_for(database)
    engine = LifecycleEngine(repo, database)
    assert admit(scheduler) == ["S1"]
    status, _coder, _reviewer, _docs = carry(scheduler, engine, risky=True)
    assert status == "awaiting_human_approval"
    assert scheduler.snapshot()["S1"][0] == "awaiting_human_approval"
    latest = engine.store.latest_evaluation("DEV-S1-A1", 1)
    assert latest["status"] == "awaiting_human_approval" and latest["requires_human"]
    assert scheduler.snapshot()["S2"][0] == "pending"


def test_retry_after_blocked_gate_starts_a_new_attempt(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    write_policy(database, "require_acceptance_criteria: false\n")
    scheduler = scheduler_for(database)
    engine = LifecycleEngine(repo, database)
    assert admit(scheduler) == ["S1"]
    assert carry(scheduler, engine, security_command=FAIL_SECURITY)[0] == "blocked"
    assert scheduler.snapshot()["S1"] == ("blocked", 1)
    scheduler.retry("S1", DispatchBudget())
    assert admit(scheduler) == ["S1"]
    assert scheduler.snapshot()["S1"][1] == 2
    assert carry(scheduler, engine)[0] == "ready_for_pr"
    assert scheduler.snapshot()["S1"] == ("ready_for_pr", 2)
    first = [row for row in engine.store.evidence_for("DEV-S1-A1", 1) if row.gate == "security"][-1]
    second = [row for row in engine.store.evidence_for("DEV-S1-A2", 2) if row.gate == "security"][-1]
    assert first.exit_code == 2 and second.exit_code == 0
    assert engine.store.get("DEV-S1-A2").status == "ready_for_pr"


def test_interrupt_resumes_same_attempt_and_skips_passed_gates(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    write_policy(database, "require_acceptance_criteria: false\n")
    scheduler = scheduler_for(database)
    engine = LifecycleEngine(repo, database)
    assert admit(scheduler) == ["S1"]
    task_id = "DEV-S1-A1"
    scheduler.store.transition(task_id, "awaiting_documentation", provider="claude", workspace=str(repo))
    for gate in ("tests", "security"):
        scheduler.store.record_evidence(
            task_id, gate, "command_gate", passed=True, attempt=1, exit_code=0, detail="pre-recorded")
    scheduler.store.record_evidence(
        task_id, "review", "independent_review", passed=True, attempt=1,
        detail="Checked.\nAPPROVED", identity="codex")
    write_change(repo)
    counter = tmp_path / "test-runs"
    script = tmp_path / "count_tests.py"
    script.write_text(
        "from pathlib import Path\n"
        f"path = Path(r'{counter}')\n"
        "count = int(path.read_text()) if path.exists() else 0\n"
        "path.write_text(str(count + 1))\n",
        encoding="utf-8")
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with sqlite3.connect(database) as db:
        db.execute("UPDATE dispatches SET claimed_at=? WHERE story_id='S1'", (old,))
    assert scheduler.recover_expired(DispatchBudget()) == 1
    assert scheduler.snapshot()["S1"][0] == "interrupted"
    assert scheduler.claim_resumable(DispatchBudget()) == ["S1"]
    assert scheduler.snapshot()["S1"] == ("claimed", 1)
    assert BudgetLedger(database).report("DEV")["entries"] == 1
    status, coder, reviewer, _docs = carry(
        scheduler, engine, test_command=[sys.executable, str(script)])
    assert status == "ready_for_pr"
    assert scheduler.snapshot()["S1"] == ("ready_for_pr", 1)
    assert coder.return_value.execute.call_count == 0
    assert counter.read_text(encoding="utf-8") == "1"
    assert reviewer.return_value.execute.call_count == 2
    security = [row for row in engine.store.evidence_for(task_id, 1) if row.gate == "security"]
    reviews = [row for row in engine.store.evidence_for(task_id, 1) if row.gate == "review"]
    assert [row.detail for row in security][0] == "pre-recorded" and len(security) == 2
    assert len(reviews) == 2
    assert BudgetLedger(database).report("DEV")["entries"] == 1


def test_running_agent_is_not_resumed(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    write_policy(database)
    scheduler = scheduler_for(database)
    assert admit(scheduler) == ["S1"]
    scheduler.store.transition("DEV-S1-A1", "running", provider="claude", workspace=str(repo))
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with sqlite3.connect(database) as db:
        db.execute("UPDATE dispatches SET claimed_at=? WHERE story_id='S1'", (old,))
    assert scheduler.recover_expired(DispatchBudget()) == 1
    assert scheduler.claim_resumable(DispatchBudget()) == []
    assert scheduler.snapshot()["S1"] == ("interrupted", 1)


def test_lost_heartbeat_leaves_the_claim(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    write_policy(database, "require_acceptance_criteria: false\n")
    scheduler = scheduler_for(database)
    engine = LifecycleEngine(repo, database)
    assert admit(scheduler) == ["S1"]
    calls = {"n": 0}

    def heartbeat():
        calls["n"] += 1
        return calls["n"] == 1

    with pytest.raises(ClaimLost):
        carry(scheduler, engine, heartbeat=heartbeat)
    assert scheduler.snapshot()["S1"][0] == "claimed"
    assert calls["n"] >= 2
    assert engine.store.latest_evaluation("DEV-S1-A1", 1) is None


def test_no_policy_stops_at_awaiting_validation(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    scheduler = scheduler_for(database)
    engine = LifecycleEngine(repo, database)
    assert admit(scheduler) == ["S1"]
    with patch("codehelm.orchestrator.lifecycle.command_gate") as gates, \
         patch("codehelm.quality.gates.CLIProvider") as reviewer:
        status, _coder, _reviewer, _docs = carry(scheduler, engine)
    assert status == "awaiting_validation"
    assert scheduler.snapshot()["S1"] == ("awaiting_validation", 1)
    assert engine.store.get("DEV-S1-A1").status == "awaiting_validation"
    assert gates.call_count == 0
    assert reviewer.return_value.execute.call_count == 0
    with sqlite3.connect(database) as db:
        db.execute("UPDATE dispatches SET state='interrupted' WHERE story_id='S1'")
    assert scheduler.claim_resumable(DispatchBudget()) == []
    assert scheduler.snapshot()["S1"][0] == "interrupted"


def test_failed_agent_does_not_run_gates(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    write_policy(database)
    scheduler = scheduler_for(database)
    engine = LifecycleEngine(repo, database)
    assert admit(scheduler) == ["S1"]

    def fail(prompt, cwd, timeout=600):
        return AgentResult("claude", 1, "", "boom")

    with patch("codehelm.orchestrator.lifecycle.CLIProvider") as coder, \
         patch("codehelm.quality.gates.CLIProvider") as reviewer:
        coder.return_value.execute.side_effect = fail
        status = scheduler.carry_story(
            "S1", engine, prompt="Implement S1", provider="claude",
            test_command=PASS, security_command=PASS, reviewer="codex")
    assert status == "failed"
    assert scheduler.snapshot()["S1"] == ("failed", 1)
    assert reviewer.return_value.execute.call_count == 0
    assert engine.store.latest_evaluation("DEV-S1-A1", 1) is None


def test_wave_respects_concurrency_and_budget(tmp_path):
    raw = plan_json((), (), ())
    limited = tmp_path / "budget.db"
    write_policy(limited, "require_acceptance_criteria: false\n")
    budgeted = scheduler_for(limited, raw)
    engine = LifecycleEngine(make_repo(tmp_path / "repo-budget"), limited)
    assert admit(budgeted, tokens=150) == ["S1"]
    assert budgeted.snapshot()["S2"] == ("pending", 0)
    assert carry(budgeted, engine)[0] == "ready_for_pr"
    assert engine.store.get("DEV-S2-A1") is None

    wide = tmp_path / "wide.db"
    write_policy(wide, "require_acceptance_criteria: false\n")
    scheduler = scheduler_for(wide, raw)
    wide_engine = LifecycleEngine(make_repo(tmp_path / "repo-wide"), wide)
    assert admit(scheduler, DispatchBudget(max_active=2)) == ["S1", "S2"]
    assert scheduler.snapshot()["S3"] == ("pending", 0)
    assert carry(scheduler, wide_engine, "S1")[0] == "ready_for_pr"
    assert carry(scheduler, wide_engine, "S2")[0] == "ready_for_pr"
    assert scheduler.snapshot()["S3"] == ("pending", 0)


def test_resume_capacity_follows_max_active(tmp_path):
    database = tmp_path / "state.db"
    write_policy(database)
    raw = plan_json((), ())
    scheduler = scheduler_for(database, raw)
    assert admit(scheduler, DispatchBudget(max_active=2)) == ["S1", "S2"]
    for story in ("S1", "S2"):
        scheduler.store.transition(
            f"DEV-{story}-A1", "awaiting_validation", provider="claude", workspace=str(tmp_path))
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with sqlite3.connect(database) as db:
        db.execute("UPDATE dispatches SET claimed_at=?", (old,))
    assert scheduler.recover_expired(DispatchBudget()) == 2
    assert scheduler.claim_resumable(DispatchBudget(max_active=1)) == ["S1"]
    assert scheduler.snapshot()["S2"][0] == "interrupted"


def test_schedule_completed_is_refused_when_policy_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = make_repo(tmp_path / "repo")
    plan = tmp_path / "plan.json"
    plan.write_text(plan_json((), ("S1",)), encoding="utf-8")
    state = tmp_path / ".codehelm"
    state.mkdir()
    (state / "quality.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    runner = CliRunner()
    with patch("codehelm.orchestrator.lifecycle.CLIProvider") as coder:
        result = runner.invoke(app, [
            "schedule", "--plan-file", str(plan), "--repo", str(repo),
            "--completed", "S1", "--execute"])
    assert result.exit_code == 1
    assert "refused" in result.output
    assert "quality policy" in result.output
    assert coder.return_value.execute.call_count == 0
    dry = runner.invoke(app, ["schedule", "--plan-file", str(plan), "--repo", str(repo)])
    assert dry.exit_code == 0, dry.output
    assert "S1" in dry.output


def test_schedule_completed_without_policy_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = make_repo(tmp_path / "repo")
    plan = tmp_path / "plan.json"
    plan.write_text(plan_json((), ("S1",)), encoding="utf-8")
    result = CliRunner().invoke(app, [
        "schedule", "--plan-file", str(plan), "--repo", str(repo), "--completed", "S1"])
    assert result.exit_code == 0, result.output
    assert "S2" in result.output
    assert "Dry run" in result.output


def _durable_args(plan: Path, repo: Path, **extra):
    args = [
        "schedule-durable", "--plan-file", str(plan), "--prefix", "DEV", "--repo", str(repo),
        "--provider", "claude", "--reserve-tokens", "100", "--reserve-usd", "0.10",
        "--limit-tokens", "100000", "--limit-usd", "10", "--execute",
    ]
    for key, value in extra.items():
        args.extend([f"--{key.replace('_', '-')}", value])
    return args


def test_cli_durable_wave_reaches_ready_for_pr(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = make_repo(tmp_path / "repo")
    plan = tmp_path / "plan.json"
    plan.write_text(plan_json((), ("S1",)), encoding="utf-8")
    state = tmp_path / ".codehelm"
    state.mkdir()
    (state / "quality.yaml").write_text(
        "schema_version: 1\nrequire_acceptance_criteria: false\n", encoding="utf-8")
    script = tmp_path / "ok.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    with patch("codehelm.orchestrator.lifecycle.CLIProvider") as coder, \
         patch("codehelm.quality.gates.CLIProvider") as reviewer, \
         patch("codehelm.agents.documentation.CLIProvider") as docs:
        coder.return_value.execute.side_effect = implement
        reviewer.return_value.execute.return_value = approved()
        docs.return_value.execute.side_effect = document
        result = CliRunner().invoke(app, _durable_args(
            plan, repo, reviewer="codex", test_cmd=str(script), security_cmd=str(script)))
    assert result.exit_code == 0, result.output
    assert "S1: ready_for_pr" in result.output
    scheduler = scheduler_for(state / "state.db")
    assert scheduler.snapshot()["S1"] == ("ready_for_pr", 1)
    assert scheduler.snapshot()["S2"] == ("pending", 0)


def test_cli_same_reviewer_does_not_claim(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = make_repo(tmp_path / "repo")
    plan = tmp_path / "plan.json"
    plan.write_text(plan_json(()), encoding="utf-8")
    state = tmp_path / ".codehelm"
    state.mkdir()
    (state / "quality.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    result = CliRunner().invoke(app, _durable_args(plan, repo, reviewer="claude"))
    assert result.exit_code == 1
    assert "reviewer" in result.output
    scheduler = scheduler_for(state / "state.db", plan_json(()))
    assert scheduler.snapshot()["S1"] == ("pending", 0)


def test_cli_no_policy_durable_stays_at_awaiting_validation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = make_repo(tmp_path / "repo")
    plan = tmp_path / "plan.json"
    plan.write_text(plan_json(()), encoding="utf-8")
    with patch("codehelm.orchestrator.lifecycle.CLIProvider") as coder, \
         patch("codehelm.quality.gates.CLIProvider") as reviewer:
        coder.return_value.execute.side_effect = implement
        result = CliRunner().invoke(app, _durable_args(plan, repo))
    assert result.exit_code == 0, result.output
    assert "S1: awaiting_validation" in result.output
    assert reviewer.return_value.execute.call_count == 0


def test_cli_resumes_interrupted_quality_pass(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = make_repo(tmp_path / "repo")
    plan = tmp_path / "plan.json"
    raw = plan_json((), ("S1",))
    plan.write_text(raw, encoding="utf-8")
    state = tmp_path / ".codehelm"
    state.mkdir()
    (state / "quality.yaml").write_text(
        "schema_version: 1\nrequire_acceptance_criteria: false\n", encoding="utf-8")
    database = state / "state.db"
    scheduler = scheduler_for(database, raw)
    assert admit(scheduler) == ["S1"]
    task_id = "DEV-S1-A1"
    scheduler.store.transition(task_id, "awaiting_documentation", provider="claude", workspace=str(repo))
    for gate in ("tests", "security"):
        scheduler.store.record_evidence(
            task_id, gate, "command_gate", passed=True, attempt=1, exit_code=0, detail="pre-recorded")
    scheduler.store.record_evidence(
        task_id, "review", "independent_review", passed=True, attempt=1,
        detail="Checked.\nAPPROVED", identity="codex")
    write_change(repo)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with sqlite3.connect(database) as db:
        db.execute("UPDATE dispatches SET claimed_at=? WHERE story_id='S1'", (old,))
    script = tmp_path / "ok.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    recovered = CliRunner().invoke(app, [
        "schedule-recover", "--plan-file", str(plan), "--prefix", "DEV"])
    assert recovered.exit_code == 0, recovered.output
    assert "Marked 1" in recovered.output
    with patch("codehelm.orchestrator.lifecycle.CLIProvider") as coder, \
         patch("codehelm.quality.gates.CLIProvider") as reviewer, \
         patch("codehelm.agents.documentation.CLIProvider") as docs:
        coder.return_value.execute.side_effect = implement
        reviewer.return_value.execute.return_value = approved()
        docs.return_value.execute.side_effect = document
        result = CliRunner().invoke(app, _durable_args(
            plan, repo, reviewer="codex", test_cmd=str(script), security_cmd=str(script)))
    assert result.exit_code == 0, result.output
    assert "S1: ready_for_pr" in result.output
    assert coder.return_value.execute.call_count == 0
    again = scheduler_for(database, raw)
    assert again.snapshot()["S1"] == ("ready_for_pr", 1)
    assert BudgetLedger(database).report("DEV")["entries"] == 1


def test_schedule_quality_does_not_rerun_checks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.json"
    raw = plan_json(())
    plan.write_text(raw, encoding="utf-8")
    state = tmp_path / ".codehelm"
    state.mkdir()
    (state / "quality.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    database = state / "state.db"
    scheduler = scheduler_for(database, raw, prefix="PLAN")
    scheduler.claim(DispatchBudget())
    scheduler.finish("S1", "awaiting_validation")
    scheduler.store.transition("PLAN-S1-A1", "awaiting_validation", provider="claude")
    with patch("codehelm.orchestrator.lifecycle.command_gate") as gates, \
         patch("codehelm.quality.gates.CLIProvider") as reviewer:
        result = CliRunner().invoke(app, [
            "schedule-quality", "S1", "--plan-file", str(plan), "--prefix", "PLAN", "--execute"])
    assert result.exit_code == 1
    assert "blocked" in result.output
    assert gates.call_count == 0
    assert reviewer.return_value.execute.call_count == 0
