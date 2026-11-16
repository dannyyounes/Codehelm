"""Acceptance evidence comes from named test-report lines, not a green exit code."""
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from codehelm.orchestrator.lifecycle import LifecycleEngine, TaskStore
from codehelm.providers.adapters import AgentResult
from codehelm.quality.contract import (
    QualityPolicy, assemble_evidence, parse_acceptance_report, read_acceptance_report,
)


def git(repo, *args):
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


def test_parser_keeps_named_lines_and_lets_failed_win():
    text = "\n".join([
        "noise",
        "CODEHELM_ACCEPTANCE\tWorks\tpassed",
        "CODEHELM_ACCEPTANCE\tWorks\tfailed",
        "CODEHELM_ACCEPTANCE\tStays up\tpassed",
        "CODEHELM_ACCEPTANCE\t\tpassed",
        "CODEHELM_ACCEPTANCE Works passed",
    ])
    assert parse_acceptance_report(text) == {"Works": False, "Stays up": True}
    assert parse_acceptance_report("") == {}


def test_oversized_report_file_is_ignored(tmp_path):
    path = tmp_path / ".codehelm" / "acceptance-report.txt"
    path.parent.mkdir()
    path.write_text("CODEHELM_ACCEPTANCE\tWorks\tpassed\n" + ("x" * 70000), encoding="utf-8")
    assert read_acceptance_report(tmp_path) is None


def test_exit_zero_without_lines_records_no_acceptance(tmp_path):
    repo = make_repo(tmp_path / "repo")
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    engine.store.transition("DEV-1", "awaiting_validation", provider="claude", workspace=str(repo))
    with patch("codehelm.quality.gates.CLIProvider") as reviewer:
        reviewer.return_value.execute.return_value = AgentResult("codex", 0, "APPROVED", "")
        engine.validate("DEV-1", ["python", "-c", "pass"], ["python", "-c", "pass"], "codex")
    acceptance = [row for row in engine.store.evidence_for("DEV-1", 1) if row.gate == "acceptance"]
    assert acceptance == []


def test_stdout_line_verifies_only_the_named_criterion(tmp_path):
    repo = make_repo(tmp_path / "repo")
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    engine.store.transition("DEV-1", "awaiting_validation", provider="claude", workspace=str(repo))
    command = ["python", "-c", "print('CODEHELM_ACCEPTANCE\\tWorks\\tpassed')"]
    with patch("codehelm.quality.gates.CLIProvider") as reviewer:
        reviewer.return_value.execute.return_value = AgentResult("codex", 0, "APPROVED", "")
        engine.validate("DEV-1", command, ["python", "-c", "pass"], "codex")
    rows = [row for row in engine.store.evidence_for("DEV-1", 1)
            if row.gate == "acceptance" and row.source == "test_report"]
    assert [(row.detail, row.passed) for row in rows] == [("Works", True)]
    evidence = assemble_evidence(
        engine.store, "DEV-1", 1, QualityPolicy(), repo, ["Works", "Stays up"], "S1", "claude")
    assert evidence.verified_criteria == ["Works"]


def test_prewritten_report_file_is_ignored(tmp_path):
    repo = make_repo(tmp_path / "repo")
    report = repo / ".codehelm" / "acceptance-report.txt"
    report.parent.mkdir()
    report.write_text("CODEHELM_ACCEPTANCE\tWorks\tpassed\n", encoding="utf-8")
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    engine.store.transition("DEV-1", "awaiting_validation", provider="claude", workspace=str(repo))
    with patch("codehelm.quality.gates.CLIProvider") as reviewer:
        reviewer.return_value.execute.return_value = AgentResult("codex", 0, "APPROVED", "")
        engine.validate("DEV-1", ["python", "-c", "pass"], ["python", "-c", "pass"], "codex")
    assert not [row for row in engine.store.evidence_for("DEV-1", 1) if row.gate == "acceptance"]


def test_report_file_written_by_the_command_counts(tmp_path):
    repo = make_repo(tmp_path / "repo")
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    engine.store.transition("DEV-1", "awaiting_validation", provider="claude", workspace=str(repo))
    script = (
        "from pathlib import Path; "
        "p = Path('.codehelm'); p.mkdir(exist_ok=True); "
        "p.joinpath('acceptance-report.txt').write_text("
        "'CODEHELM_ACCEPTANCE\\tWorks\\tpassed\\n')"
    )
    with patch("codehelm.quality.gates.CLIProvider") as reviewer:
        reviewer.return_value.execute.return_value = AgentResult("codex", 0, "APPROVED", "")
        engine.validate("DEV-1", ["python", "-c", script], ["python", "-c", "pass"], "codex")
    rows = [row for row in engine.store.evidence_for("DEV-1", 1) if row.gate == "acceptance"]
    assert [(row.source, row.detail, row.passed) for row in rows] == [("test_report", "Works", True)]


def test_nonzero_exit_does_not_verify_a_passed_line(tmp_path):
    repo = make_repo(tmp_path / "repo")
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    engine.store.transition("DEV-1", "awaiting_validation", provider="claude", workspace=str(repo))
    command = ["python", "-c", "print('CODEHELM_ACCEPTANCE\\tWorks\\tpassed'); raise SystemExit(1)"]
    assert not engine.validate("DEV-1", command, ["python", "-c", "pass"], "codex")
    rows = [row for row in engine.store.evidence_for("DEV-1", 1) if row.gate == "acceptance"]
    assert [(row.detail, row.passed) for row in rows] == [("Works", False)]
    assert engine.store.get("DEV-1").status == "blocked"


def test_failed_line_and_stdout_disagreement_do_not_verify(tmp_path):
    repo = make_repo(tmp_path / "repo")
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    engine.store.transition("DEV-1", "awaiting_validation", provider="claude", workspace=str(repo))
    script = (
        "from pathlib import Path; "
        "p = Path('.codehelm'); p.mkdir(exist_ok=True); "
        "p.joinpath('acceptance-report.txt').write_text("
        "'CODEHELM_ACCEPTANCE\\tWorks\\tpassed\\n'); "
        "print('CODEHELM_ACCEPTANCE\\tWorks\\tfailed')"
    )
    with patch("codehelm.quality.gates.CLIProvider") as reviewer:
        reviewer.return_value.execute.return_value = AgentResult("codex", 0, "APPROVED", "")
        engine.validate("DEV-1", ["python", "-c", script], ["python", "-c", "pass"], "codex")
    rows = [row for row in engine.store.evidence_for("DEV-1", 1) if row.gate == "acceptance"]
    assert [(row.detail, row.passed) for row in rows] == [("Works", False)]


def test_failed_report_does_not_cancel_a_human_attestation(tmp_path):
    store = TaskStore(tmp_path / "state.db")
    store.initialize()
    store.record_evidence(
        "DEV-1", "acceptance", "test_report", passed=True, attempt=1, detail="Works")
    store.record_evidence(
        "DEV-1", "acceptance", "test_report", passed=False, attempt=1, detail="Works")
    cancelled = assemble_evidence(
        store, "DEV-1", 1, QualityPolicy(), None, ["Works"], "S1", "claude")
    assert cancelled.verified_criteria == []
    store.record_evidence(
        "DEV-1", "acceptance", "human", passed=True, attempt=1, detail="Works")
    evidence = assemble_evidence(
        store, "DEV-1", 1, QualityPolicy(), None, ["Works"], "S1", "claude")
    assert evidence.verified_criteria == ["Works"]
    store.record_evidence(
        "DEV-1", "acceptance", "human", passed=True, attempt=1, detail="Stays up")
    without_human_for_works = assemble_evidence(
        store, "DEV-1", 1, QualityPolicy(), None, ["Works", "Stays up"], "S1", "claude")
    assert without_human_for_works.verified_criteria == ["Stays up", "Works"]


def test_test_report_source_is_allowed_and_agent_source_is_rejected(tmp_path):
    store = TaskStore(tmp_path / "state.db")
    store.initialize()
    store.record_evidence(
        "DEV-1", "acceptance", "test_report", passed=True, attempt=1, detail="Works")
    with pytest.raises(ValueError, match="Untrusted"):
        store.record_evidence(
            "DEV-1", "acceptance", "agent", passed=True, attempt=1, detail="Works")


def test_no_policy_path_stays_at_awaiting_pr(tmp_path):
    repo = make_repo(tmp_path / "repo")
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    engine.store.transition("DEV-22", "awaiting_post_docs_validation",
                            provider="claude", workspace=str(repo))
    (repo / "change.txt").write_text("change\n", encoding="utf-8")
    command = ["python", "-c", "print('CODEHELM_ACCEPTANCE\\tWorks\\tpassed')"]
    with patch("codehelm.orchestrator.lifecycle.independent_review") as review:
        review.return_value.passed = True
        review.return_value.detail = "APPROVED"
        review.return_value.exit_code = 0
        assert engine.validate_after_docs("DEV-22", command, ["python", "-c", "pass"], "codex")
    assert engine.store.get("DEV-22").status == "awaiting_pr"
    named = [row for row in engine.store.evidence_for("DEV-22", 1)
             if row.gate == "acceptance" and row.source == "test_report"]
    assert named and named[-1].passed and named[-1].detail == "Works"


def test_changed_report_file_is_not_the_preseeded_bytes(tmp_path):
    repo = make_repo(tmp_path / "repo")
    report = repo / ".codehelm" / "acceptance-report.txt"
    report.parent.mkdir()
    report.write_text("CODEHELM_ACCEPTANCE\tOld\tpassed\n", encoding="utf-8")
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    engine.store.transition("DEV-1", "awaiting_validation", provider="claude", workspace=str(repo))
    script = (
        "from pathlib import Path; "
        "Path('.codehelm/acceptance-report.txt').write_text("
        "'CODEHELM_ACCEPTANCE\\tWorks\\tpassed\\n')"
    )
    with patch("codehelm.quality.gates.CLIProvider") as reviewer:
        reviewer.return_value.execute.return_value = AgentResult("codex", 0, "APPROVED", "")
        engine.validate("DEV-1", ["python", "-c", script], ["python", "-c", "pass"], "codex")
    rows = [row for row in engine.store.evidence_for("DEV-1", 1) if row.gate == "acceptance"]
    assert [(row.detail, row.passed) for row in rows] == [("Works", True)]
