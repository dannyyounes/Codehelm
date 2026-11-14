"""Minimal CLI provider adapters. No shell execution or automatic permissions bypass."""
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
from typing import Protocol
from codehelm.providers.usage import Usage, parse_cli_usage


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentResult:
    provider: str
    exit_code: int
    output: str
    error: str


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
