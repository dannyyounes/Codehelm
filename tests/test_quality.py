import json
from unittest.mock import patch
from codehelm.quality.gates import (
    GateResult, _REVIEW_PROMPTS, _STORY_CONTEXT_LIMIT, _STORY_REVIEW_INSTRUCTION,
    _VERIFICATION_NOTE,
    command_gate, independent_review,
)
from codehelm.orchestrator.lifecycle import LifecycleEngine
from codehelm.providers.adapters import AgentResult
from codehelm.cli.main import app
from typer.testing import CliRunner
import subprocess


def test_empty_command_fails_closed(tmp_path):
    assert not command_gate(tmp_path, []).passed


def test_failed_command_blocks(tmp_path):
    assert not command_gate(tmp_path, ["python", "-c", "raise SystemExit(2)"]).passed


def test_reviewer_must_be_independent(tmp_path):
    assert not independent_review(tmp_path, "claude", "claude").passed


def test_reviewer_cannot_change_workspace(tmp_path):
    with patch("codehelm.quality.gates.git_snapshot", side_effect=["original", "modified"]), \
         patch("codehelm.quality.gates.CLIProvider.execute",
               return_value=AgentResult("codex", 0, "APPROVED", "")):
        assert not independent_review(tmp_path, "claude", "codex").passed


def test_review_requires_exact_verdict(tmp_path):
    with patch("codehelm.quality.gates.git_snapshot", side_effect=["original", "original"]), \
         patch("codehelm.quality.gates.CLIProvider.execute",
               return_value=AgentResult("codex", 0, "Looks good", "")):
        assert not independent_review(tmp_path, "claude", "codex").passed


def test_pipeline_passes_to_documentation_gate(tmp_path):
    from codehelm.orchestrator.lifecycle import TaskStore
    from codehelm.orchestrator.lifecycle import LifecycleEngine
    from unittest.mock import patch
    from codehelm.quality.gates import GateResult
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init"], ["config", "user.email", "test@example.org"],
                 ["config", "user.name", "Test"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "README.md").write_text("initial")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True)
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    workspace = engine.worktrees.create("DEV-9")
    engine.store.transition("DEV-9", "awaiting_validation", provider="claude", workspace=str(workspace.path))
    with patch("codehelm.orchestrator.lifecycle.independent_review",
               return_value=GateResult(True, "APPROVED")):
        assert engine.validate("DEV-9", ["python", "-c", "pass"],
                               ["python", "-c", "pass"], "codex")
    assert engine.store.get("DEV-9").status == "awaiting_documentation"


def _prompt(tmp_path, **kwargs):
    with patch("codehelm.quality.gates.git_snapshot", return_value="snap\n"), \
         patch("codehelm.quality.gates.CLIProvider") as provider:
        provider.return_value.execute.return_value = AgentResult("codex", 0, "APPROVED", "")
        result = independent_review(tmp_path, "claude", "codex", **kwargs)
    return result, provider.return_value.execute.call_args.args[0]


def test_review_prompt_unchanged_without_story_context(tmp_path):
    result, prompt = _prompt(tmp_path)
    assert result.passed and prompt == f'{_REVIEW_PROMPTS["code"]}\n\n{_VERIFICATION_NOTE}'
    assert _prompt(tmp_path, kind="architecture")[1] == (
        f'{_REVIEW_PROMPTS["architecture"]}\n\n{_VERIFICATION_NOTE}')


def test_verification_note_once_and_rest_unchanged(tmp_path):
    tail = f"\n\n{_VERIFICATION_NOTE}"
    for kind in ("code", "architecture"):
        for ctx in ({}, {"story_title": "T", "story_description": "D",
                         "acceptance_criteria": ["A"], "scope_text": "S"}):
            _r, prompt = _prompt(tmp_path, kind=kind, **ctx)
            assert prompt.count(_VERIFICATION_NOTE) == 1 and prompt.endswith(tail)
            rest = prompt[: -len(tail)]
            assert rest.endswith(_REVIEW_PROMPTS[kind])
            assert (rest == _REVIEW_PROMPTS[kind]) == (not ctx)


def test_review_prompt_includes_story_context(tmp_path):
    result, prompt = _prompt(
        tmp_path, kind="architecture", story_title="Ship context",
        story_description="Pass the story.\n\nThreat model\nhonest local workspace",
        acceptance_criteria=["Prompt includes the story"],
        scope_text="OUT OF SCOPE remote attackers")
    assert result.passed and _REVIEW_PROMPTS["architecture"] in prompt
    assert _STORY_REVIEW_INSTRUCTION in prompt
    for text in ("Story", "Ship context", "Acceptance criteria", "Prompt includes the story",
                 "Declared scope and threat model", "OUT OF SCOPE remote attackers",
                 "Threat model", "Do not request hardening", "still return APPROVED"):
        assert text in prompt


def test_story_context_is_truncated(tmp_path):
    _result, prompt = _prompt(
        tmp_path, story_title="T", story_description="D" * 30000,
        acceptance_criteria=["c"], scope_text="SCOPE")
    context, _, instruction = prompt[: -len(_REVIEW_PROMPTS["code"]) - len(_VERIFICATION_NOTE) - 4].rpartition("\n\n")
    assert instruction == _STORY_REVIEW_INSTRUCTION and len(context) == _STORY_CONTEXT_LIMIT
    assert _REVIEW_PROMPTS["code"] in prompt and "D" * (_STORY_CONTEXT_LIMIT + 1) not in prompt


def test_validate_passes_story_context(tmp_path, monkeypatch):
    monkeypatch.setattr("codehelm.orchestrator.lifecycle.WorktreeManager", lambda _path: object())
    (tmp_path / "quality.yaml").write_text(
        "schema_version: 1\nrequire_acceptance_criteria: false\n", encoding="utf-8")
    engine = LifecycleEngine(tmp_path, tmp_path / "state.db")
    engine.store.transition("DEV-9", "awaiting_validation", provider="claude", workspace=str(tmp_path))
    story = dict(story_title="Ship context", story_description="OUT OF SCOPE\nremote attackers",
                 acceptance_criteria=["Prompt includes the story"],
                 scope_text="Threat model: honest local workspace")
    passed = GateResult(True, "ok", 0)
    with patch("codehelm.orchestrator.lifecycle.command_gate", return_value=passed), \
         patch("codehelm.quality.gates.git_snapshot", return_value="snap\n"), \
         patch("codehelm.quality.gates.CLIProvider") as provider:
        provider.return_value.execute.return_value = AgentResult("codex", 0, "APPROVED", "")
        assert engine.validate("DEV-9", ["true"], ["true"], "codex", **story)
        engine.store.transition("DEV-9", "awaiting_post_docs_validation")
        engine.validate_after_docs("DEV-9", ["true"], ["true"], "codex", **story)
    prompts = [call.args[0] for call in provider.return_value.execute.call_args_list]
    assert len(prompts) == 3 and all("Ship context" in item and "OUT OF SCOPE" in item for item in prompts)
    assert any("Review the architecture" in item and "honest local workspace" in item for item in prompts)


def test_validate_cli_passes_plan_story(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".codehelm").mkdir()
    (tmp_path / ".codehelm" / "plan.json").write_text(json.dumps({
        "epic_title": "E", "epic_description": "D", "stories": [{
            "id": "S1", "title": "Bound reviewer",
            "description": "NARROWED SCOPE\nhonest local workspace",
            "acceptance_criteria": ["Prompt has the story"], "dependencies": []}]}),
        encoding="utf-8")
    with patch("codehelm.cli.main.LifecycleEngine") as engine:
        engine.return_value.validate.return_value = True
        engine.return_value.validate_after_docs.return_value = True
        engine.return_value.store.get.return_value = type("T", (), {"status": "ok", "error": ""})()
        flags = ["--reviewer", "codex", "--test-cmd", "pytest", "--security-cmd", "bandit", "--execute"]
        assert CliRunner().invoke(app, ["validate", "DEV-S1-A1", *flags]).exit_code == 0
        assert CliRunner().invoke(app, ["validate-after-docs", "DEV-S1-A1", *flags]).exit_code == 0
    for method in (engine.return_value.validate, engine.return_value.validate_after_docs):
        assert method.call_args.kwargs["story_title"] == "Bound reviewer"
        assert "NARROWED SCOPE" in method.call_args.kwargs["story_description"]
        assert method.call_args.kwargs["acceptance_criteria"] == ["Prompt has the story"]
