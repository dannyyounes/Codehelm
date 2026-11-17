import subprocess
from unittest.mock import patch

import pytest
from codehelm.agents.documentation import run_documentation_agent
from codehelm.orchestrator.durable import DispatchBudget, DurableScheduler
from codehelm.orchestrator.lifecycle import LifecycleEngine
from codehelm.planning.planner import parse_plan
from codehelm.providers.adapters import AgentResult

_DOCS_ONLY = (
    "schema_version: 1\nrequire_tests: false\nrequire_security: false\n"
    "require_review: false\nrequire_architecture: false\n"
    "require_acceptance_criteria: false\n"
)


def noop(*_args, **_kwargs):
    return AgentResult("claude", 0, "done", "")


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def workspace(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.org")
    git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("Original")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    return repo


def test_documentation_requires_actual_change(workspace):
    with patch("codehelm.agents.documentation.CLIProvider.execute",
               return_value=AgentResult("claude", 0, "done", "")):
        assert not run_documentation_agent(workspace, "claude").passed


def test_documentation_change_accepted(workspace):
    def edit(*args):
        (workspace / "README.md").write_text("Updated")
        return AgentResult("claude", 0, "done", "")
    with patch("codehelm.agents.documentation.CLIProvider.execute", side_effect=edit):
        result = run_documentation_agent(workspace, "claude")
    assert result.passed and result.detail == "done"


def test_code_change_rejected(workspace):
    def edit(*args):
        (workspace / "main.py").write_text("print(1)")
        return AgentResult("claude", 0, "done", "")
    with patch("codehelm.agents.documentation.CLIProvider.execute", side_effect=edit):
        assert not run_documentation_agent(workspace, "claude").passed


def test_documentation_lifecycle_transition(workspace, tmp_path):
    engine = LifecycleEngine(workspace, tmp_path / "state.db")
    engine.store.transition("DEV-10", "awaiting_documentation", provider="codex",
                            workspace=str(workspace))
    def edit(*args):
        (workspace / "README.md").write_text("Updated")
        return AgentResult("claude", 0, "done", "")
    with patch("codehelm.agents.documentation.CLIProvider.execute", side_effect=edit):
        assert engine.document("DEV-10", "claude")
    assert engine.store.get("DEV-10").status == "awaiting_post_docs_validation"


def test_cannot_skip_review(workspace, tmp_path):
    engine = LifecycleEngine(workspace, tmp_path / "state.db")
    engine.store.transition("DEV-11", "awaiting_validation", workspace=str(workspace))
    with pytest.raises(ValueError, match="awaiting_documentation"):
        engine.document("DEV-11", "claude")


def _prompts(workspace, **kwargs):
    seen = []

    def execute(prompt, _cwd, timeout=600):
        seen.append(prompt)
        return AgentResult("claude", 0, "done", "")

    with patch("codehelm.agents.documentation.CLIProvider.execute", side_effect=execute):
        run_documentation_agent(workspace, "claude", **kwargs)
    return seen


def test_prompt_unchanged_without_story(workspace):
    plain = _prompts(workspace)[0]
    blank = _prompts(workspace, story_title="  ", story_description="", acceptance_criteria=[" "])[0]
    assert plain == blank
    assert "Story title:" not in plain


def test_prompt_includes_story_context(workspace):
    plain = _prompts(workspace)[0]
    prompt = _prompts(
        workspace, story_title="Ship docs", story_description="Document the API",
        acceptance_criteria=["README lists the flag", "Stay under 40 lines"],
    )[0]
    assert prompt.startswith(plain)
    assert "Story title: Ship docs" in prompt
    assert "Story description: Document the API" in prompt
    assert "- README lists the flag" in prompt
    assert "- Stay under 40 lines" in prompt
    assert "Respect any documentation scope or line limits stated there" in prompt
    assert "make no edit if documentation is already complete" in prompt


@pytest.mark.parametrize("relative", ["README.md", "docs/guide.md", "notes.rst", "CHANGELOG"])
def test_already_present_documentation_passes(workspace, relative):
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("documented\n", encoding="utf-8")
    with patch("codehelm.agents.documentation.CLIProvider.execute", side_effect=noop):
        result = run_documentation_agent(workspace, "claude")
    assert result.passed
    assert result.detail == "Documentation already included in the change"


def test_no_documentation_anywhere_fails(workspace):
    (workspace / "main.py").write_text("print(1)\n", encoding="utf-8")
    with patch("codehelm.agents.documentation.CLIProvider.execute", side_effect=noop):
        result = run_documentation_agent(workspace, "claude")
    assert not result.passed
    assert result.detail == "No documentation changes detected"


def test_non_doc_edit_fails_even_when_docs_already_present(workspace):
    (workspace / "README.md").write_text("documented\n", encoding="utf-8")

    def edit(*_args, **_kwargs):
        (workspace / "main.py").write_text("print(1)\n", encoding="utf-8")
        return AgentResult("claude", 0, "done", "")

    with patch("codehelm.agents.documentation.CLIProvider.execute", side_effect=edit):
        result = run_documentation_agent(workspace, "claude")
    assert not result.passed
    assert result.detail == "Documentation agent modified non-documentation files"


def test_restoring_source_to_head_fails(workspace):
    (workspace / "main.py").write_text("print(1)\n", encoding="utf-8")
    git(workspace, "add", "main.py")
    git(workspace, "commit", "-m", "add source")
    (workspace / "README.md").write_text("documented\n", encoding="utf-8")
    (workspace / "main.py").write_text("print(2)\n", encoding="utf-8")

    def restore(*_args, **_kwargs):
        git(workspace, "checkout", "HEAD", "--", "main.py")
        return AgentResult("claude", 0, "done", "")

    with patch("codehelm.agents.documentation.CLIProvider.execute", side_effect=restore):
        result = run_documentation_agent(workspace, "claude")
    assert not result.passed
    assert result.detail == "Documentation agent modified non-documentation files"


def test_document_passes_docs_committed_against_policy_base(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "staging")
    git(repo, "config", "user.email", "test@example.org")
    git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    git(repo, "checkout", "-b", "feature")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "docs")
    (tmp_path / "quality.yaml").write_text("schema_version: 1\nbase_branch: staging\n", encoding="utf-8")
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    engine.store.transition("DEV-12", "awaiting_documentation", provider="codex", workspace=str(repo))
    with patch("codehelm.agents.documentation.CLIProvider.execute", side_effect=noop):
        assert engine.document("DEV-12", "claude")
    task = engine.store.get("DEV-12")
    assert task.status == "awaiting_post_docs_validation"
    row = engine.store.evidence_for("DEV-12", 1)[-1]
    assert row.passed and row.detail == "Documentation already included in the change"


def test_invalid_policy_blocks_instead_of_staying_in_documenting(workspace, tmp_path):
    (workspace / "README.md").write_text("coder already documented this\n", encoding="utf-8")
    (tmp_path / "quality.yaml").write_text("schema_version: 99\n", encoding="utf-8")
    engine = LifecycleEngine(workspace, tmp_path / "state.db")
    engine.store.transition("DEV-13", "awaiting_documentation", provider="codex", workspace=str(workspace))
    with patch("codehelm.agents.documentation.CLIProvider.execute", side_effect=noop):
        assert engine.document("DEV-13", "claude") is False
    task = engine.store.get("DEV-13")
    assert task.status == "blocked"
    row = engine.store.evidence_for("DEV-13", 1)[0]
    assert row.passed is False and "schema_version" in row.detail


def test_scheduler_passes_story_into_documentation_prompt(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.org")
    git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    database = tmp_path / "state.db"
    (tmp_path / "quality.yaml").write_text(_DOCS_ONLY, encoding="utf-8")
    plan = parse_plan("""{
        "epic_title": "Epic", "epic_description": "Epic body",
        "stories": [{
            "id": "S1", "title": "Ship docs", "description": "Document the API",
            "acceptance_criteria": ["README lists the flag"], "dependencies": []
        }]
    }""")
    scheduler = DurableScheduler(database, plan, "DEV")
    engine = LifecycleEngine(repo, database)
    assert scheduler.claim(DispatchBudget()) == ["S1"]
    seen = []

    def capture(prompt, _cwd, timeout=600):
        seen.append(prompt)
        return AgentResult("claude", 0, "docs", "")

    with patch("codehelm.orchestrator.lifecycle.CLIProvider") as coder, \
         patch("codehelm.agents.documentation.CLIProvider") as docs:
        coder.return_value.execute.return_value = AgentResult("claude", 0, "done", "")
        docs.return_value.execute.side_effect = capture
        scheduler.carry_story("S1", engine, prompt="Implement S1", provider="claude", docs_provider="claude")
    prompt = seen[0]
    assert "Story title: Ship docs" in prompt
    assert "Story description: Document the API" in prompt
    assert "- README lists the flag" in prompt
    assert "make no edit if documentation is already complete" in prompt
