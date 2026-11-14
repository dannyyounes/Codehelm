from pathlib import Path
from unittest.mock import patch
import pytest
from typer.testing import CliRunner
from codehelm.cli.main import app
from codehelm.providers.adapters import CLIProvider, ProviderError


def test_provider_names():
    assert set(CLIProvider.COMMANDS) == {"claude", "codex", "gemini"}


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
