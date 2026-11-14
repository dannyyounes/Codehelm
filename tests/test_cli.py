from typer.testing import CliRunner
from codehelm.cli.main import app

runner = CliRunner()

def test_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "CodeHelm" in result.output

def test_init_and_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"]).exit_code == 0
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Stored tasks: 0" in result.output

def test_run_requires_prompt():
    result = runner.invoke(app, ["run", "--jira", "DEV-123"])
    assert result.exit_code == 2
    assert "Missing option" in result.output
