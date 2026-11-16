"""Opt-in HTTP provider transport. These tests never open a socket."""
import email
import io
import json
import re
import sqlite3
import subprocess
import urllib.error
from decimal import Decimal
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.orchestrator.budgets import BudgetLedger, Limits
from codehelm.orchestrator.durable import DispatchBudget, DurableScheduler
from codehelm.orchestrator.lifecycle import LifecycleEngine
from codehelm.planning.planner import parse_plan
from codehelm.providers.adapters import CLIProvider, HTTPProvider, ProviderError

TOKEN = "secret-token-value"
URL = "https://example.test/v1/messages"
PROMPT = "fix the tests"
ENV = {
    "claude": ("CODEHELM_CLAUDE_API_URL", "CODEHELM_CLAUDE_API_TOKEN", "CODEHELM_CLAUDE_MODEL"),
    "codex": ("CODEHELM_CODEX_API_URL", "CODEHELM_CODEX_API_TOKEN", "CODEHELM_CODEX_MODEL"),
    "gemini": ("CODEHELM_GEMINI_API_URL", "CODEHELM_GEMINI_API_TOKEN", "CODEHELM_GEMINI_MODEL"),
}


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, status=200, body=b"{}", error=None):
        self.status = status
        self.body = body
        self.error = error
        self.requests = []
        self.timeouts = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return FakeResponse(self.status, self.body)


def configure(monkeypatch, provider, url=URL, token=TOKEN, model=None):
    for keys in ENV.values():
        for key in keys:
            monkeypatch.delenv(key, raising=False)
    url_key, token_key, model_key = ENV[provider]
    if url is not None:
        monkeypatch.setenv(url_key, url)
    if token is not None:
        monkeypatch.setenv(token_key, token)
    if model is not None:
        monkeypatch.setenv(model_key, model)
    else:
        monkeypatch.setenv(model_key, f"{provider}-model")


def success_payload(provider, text, usage=None, cost=None):
    if provider == "claude":
        payload = {"content": [{"type": "text", "text": text}]}
    elif provider == "codex":
        payload = {"output_text": text}
    else:
        payload = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    if usage is not None:
        payload["usage"] = usage
    if cost is not None:
        payload["total_cost_usd"] = cost
    return json.dumps(payload).encode()


def expected_body(provider, prompt):
    model = f"{provider}-model"
    if provider == "claude":
        return {
            "model": model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        }
    if provider == "codex":
        return {"model": model, "input": prompt}
    return {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}


def header_map(request):
    return {key.lower(): value for key, value in request.header_items()}


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_repo(path):
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "test@example.org")
    git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "initial")
    return path


def plan():
    return parse_plan(json.dumps({
        "epic_title": "Feature",
        "epic_description": "Build it",
        "stories": [{
            "id": "S1",
            "title": "First",
            "description": "First story",
            "acceptance_criteria": ["Works"],
            "dependencies": [],
        }],
    }))


class Result:
    returncode = 0
    stdout = "done"
    stderr = ""


def _run_or_real(calls):
    real_run = subprocess.run

    def fake_run(args, *pos, **kwargs):
        command = args[0] if isinstance(args, (list, tuple)) and args else None
        if command in {"claude", "codex", "gemini"}:
            calls.append((list(args), kwargs))
            return Result()
        return real_run(args, *pos, **kwargs)

    return fake_run


def test_default_transport_builds_cli_argv_and_does_not_open_a_socket(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    configure(monkeypatch, "claude")
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    calls = []
    with patch("subprocess.run", side_effect=_run_or_real(calls)), \
         patch("codehelm.providers.adapters.shutil.which", return_value="/usr/bin/claude"), \
         patch("codehelm.providers.adapters.urllib.request.build_opener") as opener, \
         patch("codehelm.orchestrator.lifecycle.HTTPProvider", side_effect=AssertionError("http")), \
         patch("socket.socket", side_effect=AssertionError("socket opened")), \
         patch("socket.create_connection", side_effect=AssertionError("socket opened")):
        result = engine.execute("DEV-1", "Fix bug", "claude")
    assert result.output == "done"
    assert calls == [(["claude", "-p", "Fix bug"], calls[0][1])]
    assert calls[0][1]["shell"] is False
    opener.assert_not_called()
    direct_calls = []
    with patch("subprocess.run", side_effect=_run_or_real(direct_calls)), \
         patch("codehelm.providers.adapters.shutil.which", return_value="/usr/bin/claude"), \
         patch("socket.socket", side_effect=AssertionError("socket opened")):
        CLIProvider("claude").execute("Fix bug", tmp_path)
    assert direct_calls[0][0] == ["claude", "-p", "Fix bug"]
    assert direct_calls[0][1]["shell"] is False


@pytest.mark.parametrize("missing", ["url", "token", "model"])
def test_http_missing_setting_raises_and_does_not_call_cli(tmp_path, monkeypatch, missing):
    configure(
        monkeypatch, "claude",
        url=None if missing == "url" else URL,
        token=None if missing == "token" else TOKEN,
        model=None if missing == "model" else "claude-model")
    if missing == "model":
        monkeypatch.delenv("CODEHELM_CLAUDE_MODEL", raising=False)
    with patch("codehelm.providers.adapters.subprocess.run") as run, \
         patch("codehelm.providers.adapters.urllib.request.build_opener") as opener, \
         patch("socket.socket", side_effect=AssertionError("socket opened")):
        with pytest.raises(ProviderError) as caught:
            HTTPProvider("claude").execute(PROMPT, tmp_path)
    assert TOKEN not in str(caught.value)
    run.assert_not_called()
    opener.assert_not_called()


def test_http_engine_missing_token_does_not_fall_back_to_cli(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    configure(monkeypatch, "claude", token=None)
    monkeypatch.delenv("CODEHELM_CLAUDE_API_TOKEN", raising=False)
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    real_run = subprocess.run

    def refuse_cli(args, *pos, **kwargs):
        command = args[0] if isinstance(args, (list, tuple)) and args else None
        if command in {"claude", "codex", "gemini"}:
            raise AssertionError(f"CLI launched: {args}")
        return real_run(args, *pos, **kwargs)

    with patch("codehelm.orchestrator.lifecycle.CLIProvider", side_effect=AssertionError("cli")), \
         patch("subprocess.run", side_effect=refuse_cli), \
         patch("codehelm.providers.adapters.urllib.request.build_opener") as opener:
        with pytest.raises(ProviderError) as caught:
            engine.execute("DEV-2", PROMPT, "claude", transport="http")
    assert TOKEN not in str(caught.value)
    opener.assert_not_called()
    assert engine.store.get("DEV-2").status == "failed"


@pytest.mark.parametrize("provider", ["claude", "codex", "gemini"])
def test_each_provider_posts_documented_json_and_reads_text(tmp_path, monkeypatch, provider):
    configure(monkeypatch, provider)
    opener = FakeOpener(body=success_payload(provider, f"hello from {provider}"))
    with patch("codehelm.providers.adapters.urllib.request.build_opener", return_value=opener), \
         patch("codehelm.providers.adapters.subprocess.run") as run, \
         patch("socket.socket", side_effect=AssertionError("socket opened")):
        result = HTTPProvider(provider).execute(PROMPT, tmp_path, timeout=17)
    run.assert_not_called()
    assert result.output == f"hello from {provider}"
    assert result.exit_code == 0
    assert result.usage is None
    request = opener.requests[0]
    assert request.get_method() == "POST"
    assert request.full_url == URL
    assert json.loads(request.data.decode()) == expected_body(provider, PROMPT)
    assert opener.timeouts == [17]
    headers = header_map(request)
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in request.full_url
    assert TOKEN not in request.data.decode()
    if provider == "claude":
        assert headers["anthropic-version"] == "2023-06-01"
    else:
        assert "anthropic-version" not in headers


def test_claude_concatenates_text_parts(tmp_path, monkeypatch):
    configure(monkeypatch, "claude")
    body = json.dumps({"content": [{"text": "hello "}, {"text": "there"}]}).encode()
    opener = FakeOpener(body=body)
    with patch("codehelm.providers.adapters.urllib.request.build_opener", return_value=opener):
        result = HTTPProvider("claude").execute(PROMPT, tmp_path)
    assert result.output == "hello there"


def test_non_200_and_unrecognized_body_fail_without_leaking_the_token(tmp_path, monkeypatch):
    configure(monkeypatch, "claude")
    http_error = urllib.error.HTTPError(
        URL, 500, f"denied {TOKEN}", email.message.Message(),
        io.BytesIO(f"body {TOKEN}".encode()))
    error_opener = FakeOpener(error=http_error)
    with patch("codehelm.providers.adapters.urllib.request.build_opener", return_value=error_opener), \
         patch("codehelm.providers.adapters.subprocess.run") as run:
        with pytest.raises(ProviderError) as caught:
            HTTPProvider("claude").execute(PROMPT, tmp_path)
    assert TOKEN not in str(caught.value)
    assert "500" in str(caught.value)
    run.assert_not_called()

    created = FakeOpener(status=201, body=success_payload("claude", "should not be used"))
    with patch("codehelm.providers.adapters.urllib.request.build_opener", return_value=created), \
         patch("codehelm.providers.adapters.subprocess.run") as run:
        with pytest.raises(ProviderError) as caught:
            HTTPProvider("claude").execute(PROMPT, tmp_path)
    assert TOKEN not in str(caught.value)
    assert "201" in str(caught.value)
    run.assert_not_called()

    for provider, body in (
        ("claude", json.dumps({"content": TOKEN}).encode()),
        ("codex", json.dumps({"output_text": ""}).encode()),
        ("gemini", json.dumps({"candidates": []}).encode()),
        ("claude", TOKEN.encode()),
    ):
        configure(monkeypatch, provider)
        opener = FakeOpener(body=body)
        with patch("codehelm.providers.adapters.urllib.request.build_opener", return_value=opener), \
             patch("codehelm.providers.adapters.subprocess.run") as run:
            with pytest.raises(ProviderError) as caught:
                HTTPProvider(provider).execute(PROMPT, tmp_path)
        assert TOKEN not in str(caught.value)
        assert "unrecognized" in str(caught.value)
        run.assert_not_called()


def test_token_in_url_is_refused_before_the_request(tmp_path, monkeypatch):
    configure(monkeypatch, "codex", url=f"https://user:{TOKEN}@example.test/v1")
    with patch("codehelm.providers.adapters.urllib.request.build_opener") as opener, \
         patch("codehelm.providers.adapters.subprocess.run") as run:
        with pytest.raises(ProviderError) as caught:
            HTTPProvider("codex").execute(PROMPT, tmp_path)
    assert TOKEN not in str(caught.value)
    opener.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize("provider", ["claude", "codex", "gemini"])
def test_recognized_usage_is_returned_and_missing_usage_is_none(tmp_path, monkeypatch, provider):
    configure(monkeypatch, provider)
    with_usage = FakeOpener(body=success_payload(
        provider, "done", {"input_tokens": 3, "output_tokens": 4}, "0.5"))
    with patch("codehelm.providers.adapters.urllib.request.build_opener", return_value=with_usage):
        result = HTTPProvider(provider).execute(PROMPT, tmp_path)
    assert result.output == "done"
    assert result.usage is not None
    assert result.usage.input_tokens == 3
    assert result.usage.output_tokens == 4
    assert result.usage.usd == Decimal("0.5")

    without_usage = FakeOpener(body=success_payload(provider, "done"))
    with patch("codehelm.providers.adapters.urllib.request.build_opener", return_value=without_usage):
        result = HTTPProvider(provider).execute(PROMPT, tmp_path)
    assert result.output == "done"
    assert result.usage is None

    malformed = FakeOpener(body=success_payload(
        provider, "done", {"input_tokens": True, "output_tokens": 1}))
    with patch("codehelm.providers.adapters.urllib.request.build_opener", return_value=malformed):
        result = HTTPProvider(provider).execute(PROMPT, tmp_path)
    assert result.output == "done"
    assert result.usage is None


def test_http_usage_is_stored_and_unknown_usage_is_not_zero(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    limits = Limits(10_000, Decimal("20"))
    ledger = BudgetLedger(database)
    ledger.reserve("PLAN", "DEV-3", "claude", 1000, Decimal("1.50"), limits)
    ledger.reserve("PLAN", "DEV-4", "claude", 1000, Decimal("1.50"), limits)
    configure(monkeypatch, "claude")
    engine = LifecycleEngine(repo, database)
    recognized = FakeOpener(body=success_payload(
        "claude", "done", {"input_tokens": 4, "output_tokens": 6}, "0.25"))
    with patch("codehelm.providers.adapters.urllib.request.build_opener", return_value=recognized), \
         patch("codehelm.orchestrator.lifecycle.CLIProvider", side_effect=AssertionError("cli")):
        result = engine.execute("DEV-3", PROMPT, "claude", transport="http")
    assert result.usage is not None
    assert result.usage.tokens == 10
    with sqlite3.connect(database) as conn:
        row = conn.execute(
            """SELECT actual_tokens, actual_usd, status FROM budget_reservations
               WHERE prefix='PLAN' AND task_id='DEV-3'""").fetchone()
    assert row == (10, "0.25", "reconciled")

    unknown = FakeOpener(body=success_payload("claude", "plain text"))
    with patch("codehelm.providers.adapters.urllib.request.build_opener", return_value=unknown), \
         patch("codehelm.orchestrator.lifecycle.CLIProvider", side_effect=AssertionError("cli")):
        result = engine.execute("DEV-4", PROMPT, "claude", transport="http")
    assert result.usage is None
    with sqlite3.connect(database) as conn:
        tokens, usd, status, usd_type = conn.execute(
            """SELECT actual_tokens, actual_usd, status, typeof(actual_usd)
               FROM budget_reservations WHERE prefix='PLAN' AND task_id='DEV-4'""").fetchone()
    assert tokens is None
    assert usd is None
    assert usd != 0
    assert usd_type == "null"
    assert status == "reserved"


def test_carry_story_passes_transport_and_defaults_to_cli(tmp_path):
    repo = make_repo(tmp_path / "repo")
    database = tmp_path / "state.db"
    scheduler = DurableScheduler(database, plan(), "DEV")
    assert scheduler.claim(DispatchBudget()) == ["S1"]
    engine = LifecycleEngine(repo, database)
    with patch.object(engine, "execute", return_value=type("R", (), {
            "exit_code": 0, "output": "ok", "error": ""})()) as execute:
        status = scheduler.carry_story(
            "S1", engine, prompt="Implement S1", provider="claude", transport="http")
    assert status == "awaiting_validation"
    assert execute.call_args.kwargs["transport"] == "http"

    database = tmp_path / "other.db"
    scheduler = DurableScheduler(database, plan(), "DEV")
    assert scheduler.claim(DispatchBudget()) == ["S1"]
    engine = LifecycleEngine(repo, database)
    with patch.object(engine, "execute", return_value=type("R", (), {
            "exit_code": 0, "output": "ok", "error": ""})()) as execute:
        scheduler.carry_story("S1", engine, prompt="Implement S1", provider="claude")
    assert execute.call_args.kwargs["transport"] == "cli"


def test_dry_runs_do_not_call_the_network(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = make_repo(tmp_path / "repo")
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan().to_dict()), encoding="utf-8")
    runner = CliRunner()
    with patch("codehelm.providers.adapters.urllib.request.build_opener") as opener, \
         patch("urllib.request.urlopen") as urlopen, \
         patch("socket.socket", side_effect=AssertionError("socket opened")):
        agent = runner.invoke(app, [
            "agent-run", "--provider", "claude", "--prompt", "test",
            "--workspace", str(tmp_path), "--transport", "http"])
        single = runner.invoke(app, [
            "run", "--jira", "DEV-1", "--prompt", "Fix", "--transport", "http"])
        scheduled = runner.invoke(app, [
            "schedule", "--plan-file", str(plan_file), "--repo", str(repo),
            "--transport", "http"])
        durable = runner.invoke(app, [
            "schedule-durable", "--plan-file", str(plan_file), "--repo", str(repo),
            "--reserve-tokens", "10", "--reserve-usd", "1",
            "--limit-tokens", "100", "--limit-usd", "10", "--transport", "http"])
    assert agent.exit_code == 0, agent.output
    assert single.exit_code == 0, single.output
    assert scheduled.exit_code == 0, scheduled.output
    assert durable.exit_code == 0, durable.output
    assert "Dry run" in agent.output
    assert "Dry run" in single.output
    assert "Dry run" in scheduled.output
    assert "Dry run" in durable.output
    opener.assert_not_called()
    urlopen.assert_not_called()


def test_transport_choices_default_to_cli():
    from typer.main import get_command

    click_app = get_command(app)
    runner = CliRunner()
    for command in ("run", "agent-run", "schedule", "schedule-durable"):
        param = next(item for item in click_app.commands[command].params if item.name == "transport")
        assert param.opts == ["--transport"]
        assert param.default.value == "cli"
        assert {item.value for item in param.type_descriptor.annotation} == {"cli", "http"}
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, result.output
        # GitHub Actions forces a color terminal, which can put an ANSI code between the dashes.
        text = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "--transport" in text
        assert "cli|http" in text
        assert "default: cli" in text
    rejected = runner.invoke(app, [
        "run", "--jira", "DEV-1", "--prompt", "Fix", "--transport", "ssh"])
    assert rejected.exit_code != 0
    assert "ssh" in rejected.output


def test_unknown_http_provider_does_not_call_the_network():
    with patch("codehelm.providers.adapters.urllib.request.build_opener") as opener, \
         patch("codehelm.providers.adapters.subprocess.run") as run:
        with pytest.raises(ProviderError):
            HTTPProvider("unknown")
    opener.assert_not_called()
    run.assert_not_called()
