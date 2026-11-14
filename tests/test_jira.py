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
