from pathlib import Path
from unittest.mock import patch
import pytest
from typer.testing import CliRunner
from codehelm.cli.main import app
from codehelm.providers.adapters import CLIProvider, ProviderError, execute_metered


def test_provider_names():
    assert set(CLIProvider.COMMANDS) == {"claude", "codex", "gemini", "cursor"}


def test_unknown_provider():
    with pytest.raises(ProviderError):
        CLIProvider("unknown")


def test_missing_workspace(tmp_path):
    with pytest.raises(ProviderError, match="Workspace"):
        CLIProvider("claude").execute("task", tmp_path / "missing")


def test_no_shell_and_capture(tmp_path):
    class Result:
        returncode = 0
        stdout = "done"
        stderr = ""
    with patch("codehelm.providers.adapters.shutil.which", return_value="/usr/bin/claude"), \
         patch("codehelm.providers.adapters.subprocess.run", return_value=Result()) as run:
        result = CLIProvider("claude").execute("fix tests", tmp_path)
        assert result.output == "done"
        args, kwargs = run.call_args
        assert args[0] == ["claude", "-p", "fix tests"]
        assert kwargs["shell"] is False
        assert kwargs["cwd"] == tmp_path.resolve()


def test_cli_requires_execute(tmp_path):
    result = CliRunner().invoke(app, ["agent-run", "--provider", "claude", "--prompt", "test", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "Dry run" in result.output


class _Done:
    returncode = 0
    stdout = "done"
    stderr = ""


def test_cursor_argv_default_model(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEHELM_CURSOR_MODEL", raising=False)
    with patch("codehelm.providers.adapters.shutil.which", return_value="/usr/bin/cursor-agent"), \
         patch("codehelm.providers.adapters.subprocess.run", return_value=_Done()) as run:
        result = CLIProvider("cursor").execute("fix tests", tmp_path, timeout=42)
    assert result.provider == "cursor" and result.output == "done"
    args, kwargs = run.call_args
    assert args[0] == ["cursor-agent", "-p", "--trust", "--output-format", "text",
                       "--model", "auto", "--", "fix tests"]
    assert kwargs["shell"] is False and kwargs["timeout"] == 42
    assert kwargs["cwd"] == tmp_path.resolve()


def test_cursor_model_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEHELM_CURSOR_MODEL", "gpt-5")
    with patch("codehelm.providers.adapters.shutil.which", return_value="/usr/bin/cursor-agent"), \
         patch("codehelm.providers.adapters.subprocess.run", return_value=_Done()) as run:
        CLIProvider("cursor").execute("fix tests", tmp_path)
    assert run.call_args[0][0][-4:] == ["--model", "gpt-5", "--", "fix tests"]


@pytest.mark.parametrize("model", ["--force", "a b"])
def test_cursor_rejects_unsafe_model(tmp_path, monkeypatch, model):
    monkeypatch.setenv("CODEHELM_CURSOR_MODEL", model)
    with patch("codehelm.providers.adapters.shutil.which", return_value="/usr/bin/cursor-agent"), \
         patch("codehelm.providers.adapters.subprocess.run") as run:
        with pytest.raises(ProviderError, match="CODEHELM_CURSOR_MODEL"):
            CLIProvider("cursor").execute("fix tests", tmp_path)
    run.assert_not_called()


def test_cursor_missing_cli(tmp_path):
    with patch("codehelm.providers.adapters.shutil.which", return_value=None):
        with pytest.raises(ProviderError, match="cursor CLI not found"):
            CLIProvider("cursor").execute("fix tests", tmp_path)


def test_cursor_reviewer_differs_from_coder(tmp_path):
    from codehelm.quality.gates import independent_review
    verdict = independent_review(tmp_path, "cursor", "cursor")
    assert not verdict.passed and "different provider" in verdict.detail


def test_cursor_metered_uses_json(tmp_path, monkeypatch):
    from codehelm.providers.adapters import execute_metered
    monkeypatch.delenv("CODEHELM_CURSOR_MODEL", raising=False)
    with patch("codehelm.providers.adapters.shutil.which", return_value="/usr/bin/cursor-agent"), \
         patch("codehelm.providers.adapters.subprocess.run", return_value=_Done()) as run:
        result = execute_metered("cursor", "fix tests", tmp_path)
    assert run.call_args[0][0] == ["cursor-agent", "-p", "--trust", "--output-format", "json",
                                   "--model", "auto", "--", "fix tests"]
    assert result.usage is None


def test_providers_lists_cursor():
    with patch("codehelm.providers.adapters.shutil.which", return_value=None):
        result = CliRunner().invoke(app, ["providers"])
    assert "cursor: not found" in result.output


def test_cursor_prompt_not_parsed_as_option(tmp_path):
    with patch("codehelm.providers.adapters.shutil.which", return_value="/usr/bin/cursor-agent"), \
         patch("codehelm.providers.adapters.subprocess.run", return_value=_Done()) as run:
        CLIProvider("cursor").execute("--help", tmp_path)
    assert run.call_args[0][0][-2:] == ["--", "--help"]


class _Quiet:
    returncode = 0
    stdout = "{}"
    stderr = ""


def _run(monkeypatch, tmp_path, fn, env=None):
    for key in ("CODEHELM_CLAUDE_ARGS", "CODEHELM_CLAUDE_ARGS_DOCS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    with patch("codehelm.providers.adapters.shutil.which", return_value="/usr/bin/x"), \
         patch("codehelm.providers.adapters.subprocess.run", return_value=_Quiet()) as run:
        try:
            fn()
        finally:
            return_run = run
    return return_run


def _cmd(run):
    return run.call_args[0][0]


def test_claude_args_unset_keeps_command(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path, lambda: CLIProvider("claude").execute("p", tmp_path))
    assert _cmd(run) == ["claude", "-p", "p"]
    run = _run(monkeypatch, tmp_path, lambda: execute_metered("claude", "p", tmp_path))
    assert _cmd(run) == ["claude", "-p", "--output-format", "json", "p"]


def test_claude_args_set(monkeypatch, tmp_path):
    env = {"CODEHELM_CLAUDE_ARGS": "--model sonnet"}
    run = _run(monkeypatch, tmp_path, lambda: CLIProvider("claude").execute("p", tmp_path), env)
    assert _cmd(run) == ["claude", "-p", "--model", "sonnet", "--", "p"]
    run = _run(monkeypatch, tmp_path, lambda: execute_metered("claude", "p", tmp_path), env)
    assert _cmd(run) == ["claude", "-p", "--output-format", "json", "--model", "sonnet", "--", "p"]


def test_claude_args_role_precedence_and_blank_fallback(monkeypatch, tmp_path):
    env = {"CODEHELM_CLAUDE_ARGS": "--model sonnet", "CODEHELM_CLAUDE_ARGS_DOCS": "--model haiku"}
    run = _run(monkeypatch, tmp_path, lambda: CLIProvider("claude", role="docs").execute("p", tmp_path), env)
    assert _cmd(run)[2:4] == ["--model", "haiku"]
    run = _run(monkeypatch, tmp_path, lambda: execute_metered("claude", "p", tmp_path, role="docs"), env)
    assert _cmd(run)[4:6] == ["--model", "haiku"]
    env["CODEHELM_CLAUDE_ARGS_DOCS"] = "   "
    run = _run(monkeypatch, tmp_path, lambda: CLIProvider("claude", role="docs").execute("p", tmp_path), env)
    assert _cmd(run)[2:4] == ["--model", "sonnet"]
    run = _run(monkeypatch, tmp_path, lambda: execute_metered("claude", "p", tmp_path, role="docs"), env)
    assert _cmd(run)[4:6] == ["--model", "sonnet"]


@pytest.mark.parametrize("value", ["--model 'sonnet", "--dangerously-skip-permissions",
                                   "--allow-dangerously-skip-permissions",
                                   "--permission-mode=bypassPermissions"])
def test_claude_args_invalid_raises_without_launch(monkeypatch, tmp_path, value):
    env = {"CODEHELM_CLAUDE_ARGS": value}
    for fn in (lambda: CLIProvider("claude").execute("p", tmp_path),
               lambda: execute_metered("claude", "p", tmp_path)):
        def call(fn=fn):
            with pytest.raises(ProviderError, match="CODEHELM_CLAUDE_ARGS"):
                fn()
        run = _run(monkeypatch, tmp_path, call, env)
        assert not run.called


def test_claude_args_ignored_by_other_providers(monkeypatch, tmp_path):
    env = {"CODEHELM_CLAUDE_ARGS": "--model sonnet"}
    run = _run(monkeypatch, tmp_path, lambda: CLIProvider("codex").execute("p", tmp_path), env)
    assert _cmd(run) == ["codex", "exec", "p"]
    run = _run(monkeypatch, tmp_path, lambda: execute_metered("codex", "p", tmp_path), env)
    assert _cmd(run) == ["codex", "exec", "--json", "p"]
    run = _run(monkeypatch, tmp_path, lambda: execute_metered("codex", "p", tmp_path),
               {"CODEHELM_CLAUDE_ARGS": "'bad"})
    assert _cmd(run) == ["codex", "exec", "--json", "p"]
