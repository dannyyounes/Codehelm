from pathlib import Path
from unittest.mock import patch
import pytest
from typer.testing import CliRunner
from codehelm.cli.main import app
from codehelm.providers.adapters import CLIProvider, ProviderError


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
