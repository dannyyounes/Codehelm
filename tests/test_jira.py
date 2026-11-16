from unittest.mock import patch
import json
import pytest
from typer.testing import CliRunner
from codehelm.cli.main import app
from codehelm.integrations.jira import JiraClient, JiraError


@pytest.fixture
def client():
    return JiraClient("https://example.atlassian.net", "dev@example.com", "secret")


def test_reject_http_and_missing_credentials():
    with pytest.raises(JiraError):
        JiraClient("http://example.com", "dev@example.com", "secret")
    with pytest.raises(JiraError):
        JiraClient("https://example.com", "", "")


def test_create_epic(client):
    with patch.object(client, "_request", return_value={"key": "DEV-1"}) as request:
        assert client.create_issue("DEV", "New feature", "Epic")["key"] == "DEV-1"
        payload = request.call_args.args[2]
        assert payload["fields"]["issuetype"]["name"] == "Epic"


def test_story_has_parent(client):
    with patch.object(client, "_request", return_value={"key": "DEV-2"}) as request:
        client.create_issue("DEV", "Login", "Story", parent="DEV-1")
        assert request.call_args.args[2]["fields"]["parent"]["key"] == "DEV-1"


def test_bug_without_epic(client):
    with patch.object(client, "_request", return_value={"key": "DEV-3"}) as request:
        client.create_issue("DEV", "Fix crash", "Bug")
        assert "parent" not in request.call_args.args[2]["fields"]


def test_invalid_issue_type(client):
    with pytest.raises(JiraError):
        client.create_issue("DEV", "Bad", "Task")


def test_comment_uses_adf(client):
    with patch.object(client, "_request", return_value={"id": "42"}) as request:
        client.comment("DEV-1", "https://github.com/org/repo/pull/1")
        assert request.call_args.args[2]["body"]["type"] == "doc"


def test_jira_create_requires_execute():
    result = CliRunner().invoke(app, ["jira-create", "--project", "DEV",
                                      "--kind", "Epic", "--summary", "Feature"])
    assert result.exit_code == 0
    assert "Dry run" in result.output


def test_jira_link_pr_requires_execute():
    result = CliRunner().invoke(app, ["jira-link-pr", "DEV-1"])
    assert result.exit_code == 0
    assert "Dry run" in result.output


class _EmptyResponse:
    def read(self):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_transition_posts_id(client, caplog):
    with caplog.at_level(0):
        with patch("codehelm.integrations.jira.urlopen", return_value=_EmptyResponse()) as opened:
            assert client.transition("DEV/1", "31") == {}
    request = opened.call_args.args[0]
    assert request.get_method() == "POST"
    assert request.full_url == (
        "https://example.atlassian.net/rest/api/3/issue/DEV%2F1/transitions")
    assert json.loads(request.data) == {"transition": {"id": "31"}}
    assert opened.call_args.kwargs["timeout"] == 20
    assert "secret" not in caplog.text
    assert client.auth not in caplog.text
    assert "Authorization" not in caplog.text


@pytest.mark.parametrize("key,transition_id", [
    ("", "31"),
    ("   ", "31"),
    ("DEV-1", ""),
    ("DEV-1", "   "),
    ("DEV-1", "31 32"),
    ("DEV-1", "31/32"),
    ("DEV-1", "id!"),
    ("DEV-1", "id_1"),
])
def test_transition_rejects_without_network(client, key, transition_id):
    with patch("codehelm.integrations.jira.urlopen") as opened:
        with pytest.raises(JiraError):
            client.transition(key, transition_id)
    opened.assert_not_called()


def test_jira_transition_dry_run_does_not_call_client():
    with patch("codehelm.cli.main.JiraClient.from_env") as from_env:
        result = CliRunner().invoke(app, ["jira-transition", "DEV-1", "--id", "31"])
    assert result.exit_code == 0
    assert "DEV-1" in result.output
    assert "31" in result.output
    assert "Dry run" in result.output
    from_env.assert_not_called()


def test_jira_transition_execute_calls_transition():
    with patch("codehelm.cli.main.JiraClient.from_env") as from_env:
        from_env.return_value.transition.return_value = {}
        result = CliRunner().invoke(
            app, ["jira-transition", "DEV-9", "--id", "to-do", "--execute"])
    assert result.exit_code == 0
    from_env.assert_called_once_with()
    from_env.return_value.transition.assert_called_once_with("DEV-9", "to-do")
    assert "to-do" in result.output
    assert "DEV-9" in result.output


def test_jira_transition_execute_error_exits():
    with patch("codehelm.cli.main.JiraClient.from_env") as from_env:
        from_env.return_value.transition.side_effect = JiraError("refused")
        result = CliRunner().invoke(
            app, ["jira-transition", "DEV-1", "--id", "31", "--execute"])
    assert result.exit_code == 1
