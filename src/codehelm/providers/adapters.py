"""Provider adapters.

CLI adapters shell out with shell=False and remain the default. The HTTP
transport is opt-in: it has no default host, model, or token, and it never
falls back to a CLI process.
"""
from dataclasses import dataclass
from pathlib import Path
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol
from codehelm.providers.usage import Usage, parse_cli_usage, parse_usage


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentResult:
    provider: str
    exit_code: int
    output: str
    error: str
    usage: Usage | None = None


class Provider(Protocol):
    name: str
    def available(self) -> bool: ...
    def execute(self, prompt: str, cwd: Path, timeout: int = 600) -> AgentResult: ...


class CLIProvider:
    # These commands are starting points, not a guarantee of compatibility with
    # every installed CLI version. Normal provider permissions remain in force.
    COMMANDS = {
        "claude": ("claude", "-p"),
        "codex": ("codex", "exec"),
        "gemini": ("gemini", "-p"),
    }

    def __init__(self, name: str):
        if name not in self.COMMANDS:
            raise ProviderError(f"Unknown provider: {name}")
        self.name = name

    def available(self) -> bool:
        return shutil.which(self.COMMANDS[self.name][0]) is not None

    def execute(self, prompt: str, cwd: Path, timeout: int = 600) -> AgentResult:
        if not prompt.strip():
            raise ProviderError("Prompt must not be empty")
        cwd = Path(cwd).resolve()
        if not cwd.is_dir():
            raise ProviderError(f"Workspace does not exist: {cwd}")
        if not self.available():
            raise ProviderError(f"{self.name} CLI not found in PATH")
        executable, *args = self.COMMANDS[self.name]
        try:
            proc = subprocess.run(
                [executable, *args, prompt],
                cwd=cwd, capture_output=True, text=True, timeout=timeout,
                check=False, shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"{self.name} timed out after {timeout}s") from exc
        except OSError as exc:
            raise ProviderError(f"Could not launch {self.name}: {exc}") from exc
        return AgentResult(self.name, proc.returncode, proc.stdout, proc.stderr)


def provider_names() -> tuple[str, ...]:
    return tuple(CLIProvider.COMMANDS)


@dataclass(frozen=True)
class MeteredResult:
    agent: AgentResult
    usage: Usage | None


def execute_metered(provider: str, prompt: str, cwd: Path,
                    timeout: int = 600) -> MeteredResult:
    """Opt-in structured CLI execution; unsupported output leaves usage unknown."""
    adapter = CLIProvider(provider)
    if not prompt.strip():
        raise ProviderError("Prompt must not be empty")
    cwd = Path(cwd).resolve()
    if not cwd.is_dir():
        raise ProviderError("Workspace does not exist")
    if not adapter.available():
        raise ProviderError(f"{provider} CLI not found in PATH")
    commands = {
        "claude": ["claude", "-p", "--output-format", "json", prompt],
        "codex": ["codex", "exec", "--json", prompt],
        "gemini": ["gemini", "-p", prompt, "--output-format", "json"],
    }
    try:
        proc = subprocess.run(commands[provider], cwd=cwd, capture_output=True,
                              text=True, timeout=timeout, check=False, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProviderError(f"Could not execute {provider}: {exc}") from exc
    agent = AgentResult(provider, proc.returncode, proc.stdout, proc.stderr)
    return MeteredResult(agent, parse_cli_usage(provider, proc.stdout))


_HTTP_ENV = {
    "claude": ("CODEHELM_CLAUDE_API_URL", "CODEHELM_CLAUDE_API_TOKEN", "CODEHELM_CLAUDE_MODEL"),
    "codex": ("CODEHELM_CODEX_API_URL", "CODEHELM_CODEX_API_TOKEN", "CODEHELM_CODEX_MODEL"),
    "gemini": ("CODEHELM_GEMINI_API_URL", "CODEHELM_GEMINI_API_TOKEN", "CODEHELM_GEMINI_MODEL"),
}
_ANTHROPIC_VERSION = "2023-06-01"


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the bearer token is not sent to another URL."""

    def http_error_302(self, req, fp, code, msg, headers):
        raise ProviderError(f"HTTP {code} redirect refused")

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


class HTTPProvider:
    """Direct HTTP transport for claude, codex, and gemini.

    Configuration is read from the provider's environment variables only when
    this transport is used. There is no default host, model, or token, and a
    missing or invalid setting raises ProviderError without launching a process.
    """

    def __init__(self, name: str):
        if name not in _HTTP_ENV:
            raise ProviderError(f"Unknown provider: {name}")
        self.name = name

    def available(self) -> bool:
        try:
            self._settings()
        except ProviderError:
            return False
        return True

    def execute(self, prompt: str, cwd: Path, timeout: int = 600) -> AgentResult:
        if not prompt.strip():
            raise ProviderError("Prompt must not be empty")
        cwd = Path(cwd).resolve()
        if not cwd.is_dir():
            raise ProviderError(f"Workspace does not exist: {cwd}")
        url, token, model = self._settings()
        request = urllib.request.Request(
            url, data=json.dumps(self._body(model, prompt)).encode("utf-8"),
            headers=self._headers(token), method="POST")
        try:
            opener = urllib.request.build_opener(_RejectRedirect)
            with opener.open(request, timeout=timeout) as response:
                status = response.status
                raw = response.read()
        except ProviderError:
            raise
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"{self.name} HTTP {exc.code}") from None
        except Exception:
            raise ProviderError(f"{self.name} request failed") from None
        if status != 200:
            raise ProviderError(f"{self.name} HTTP {status}")
        return AgentResult(self.name, 0, *_parsed_response(self.name, raw))

    def _settings(self) -> tuple[str, str, str]:
        url_key, token_key, model_key = _HTTP_ENV[self.name]
        url = os.environ.get(url_key, "")
        token = os.environ.get(token_key, "")
        model = os.environ.get(model_key, "").strip()
        if not url.strip():
            raise ProviderError(f"Missing {url_key}")
        if not token.strip():
            raise ProviderError(f"Missing {token_key}")
        if any(char.isspace() for char in token):
            raise ProviderError(f"Invalid {token_key}")
        if not model:
            raise ProviderError(f"Missing {model_key}")
        parts = urllib.parse.urlsplit(url)
        if (any(char.isspace() for char in url) or parts.scheme not in {"http", "https"}
                or not parts.hostname or parts.username or parts.password or token in url):
            raise ProviderError(f"Invalid {url_key}")
        return url, token, model

    def _headers(self, token: str) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.name == "claude":
            headers["anthropic-version"] = _ANTHROPIC_VERSION
        return headers

    def _body(self, model: str, prompt: str) -> dict:
        if self.name == "claude":
            return {
                "model": model,
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            }
        if self.name == "codex":
            return {"model": model, "input": prompt}
        # Gemini's documented body is the contents array. The model is required
        # configuration and is not copied into this JSON or the query string.
        if not model:
            raise ProviderError("Missing CODEHELM_GEMINI_MODEL")
        return {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}


def _parsed_response(provider: str, raw: bytes) -> tuple[str, str, Usage | None]:
    try:
        text = raw.decode("utf-8")
        payload = json.loads(text)
    except (UnicodeError, json.JSONDecodeError):
        raise ProviderError(f"{provider} returned an unrecognized response") from None
    if not isinstance(payload, dict):
        raise ProviderError(f"{provider} returned an unrecognized response")
    completion = _completion_text(provider, payload)
    return completion, "", parse_usage(provider, text)


def _completion_text(provider: str, payload: dict) -> str:
    if provider == "claude":
        content = payload.get("content")
        if not isinstance(content, list) or not content:
            raise ProviderError("claude returned an unrecognized response")
        parts = []
        for item in content:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                raise ProviderError("claude returned an unrecognized response")
            parts.append(item["text"])
        text = "".join(parts)
    elif provider == "codex":
        text = payload.get("output_text")
        if not isinstance(text, str):
            raise ProviderError("codex returned an unrecognized response")
    else:
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
            raise ProviderError("gemini returned an unrecognized response")
        content = candidates[0].get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list) or not parts:
            raise ProviderError("gemini returned an unrecognized response")
        collected = []
        for part in parts:
            if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                raise ProviderError("gemini returned an unrecognized response")
            collected.append(part["text"])
        text = "".join(collected)
    if text == "":
        raise ProviderError(f"{provider} returned an unrecognized response")
    return text
