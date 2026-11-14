"""Minimal Jira Cloud REST integration. Credentials are environment-only."""
import base64
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


class JiraError(RuntimeError):
    pass


class JiraClient:
    def __init__(self, base_url: str, email: str, token: str):
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise JiraError("Jira URL must be an HTTPS host without embedded credentials")
        if not email or not token:
            raise JiraError("Jira email and API token are required")
        self.base_url = base_url.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()

    @classmethod
    def from_env(cls):
        return cls(os.getenv("CODEHELM_JIRA_URL", ""),
                   os.getenv("CODEHELM_JIRA_EMAIL", ""),
                   os.getenv("CODEHELM_JIRA_API_TOKEN", ""))

    def _request(self, method: str, path: str, data: dict | None = None) -> dict:
        request = Request(
            self.base_url + "/rest/api/3/" + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": self.auth, "Accept": "application/json",
                     "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read()
                return json.loads(body) if body else {}
        except HTTPError as exc:
            raise JiraError(f"Jira returned HTTP {exc.code}") from exc
        except URLError as exc:
            raise JiraError(f"Jira connection failed: {exc.reason}") from exc

    def issue(self, key: str) -> dict:
        return self._request("GET", "issue/" + quote(key, safe=""))

    @staticmethod
    def _adf(text: str) -> dict:
        return {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}]}

    def create_issue(self, project: str, summary: str, kind: str,
                     description: str = "", parent: str | None = None) -> dict:
        if kind not in {"Epic", "Story", "Bug"}:
            raise JiraError("Issue type must be Epic, Story or Bug")
        if not project.strip() or not summary.strip():
            raise JiraError("Project and summary are required")
        fields = {"project": {"key": project}, "summary": summary,
                  "issuetype": {"name": kind}, "description": self._adf(description)}
        if parent:
            if kind != "Story":
                raise JiraError("Parent epic is supported only for stories in this prototype")
            fields["parent"] = {"key": parent}
        return self._request("POST", "issue", {"fields": fields})

    def comment(self, key: str, message: str) -> dict:
        if not message.strip():
            raise JiraError("Comment must not be empty")
        return self._request("POST", "issue/" + quote(key, safe="") + "/comment",
                             {"body": self._adf(message)})
