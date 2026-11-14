"""Persisted single-task execution lifecycle.

The engine does not mark a story complete: validation, independent review,
documentation and PR gates are not implemented yet.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from codehelm.execution.worktrees import WorktreeManager
from codehelm.providers.adapters import CLIProvider, AgentResult
from codehelm.quality.gates import command_gate, independent_review
from codehelm.agents.documentation import run_documentation_agent
from codehelm.integrations.github_pr import publish_pr


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Task:
    id: str
    status: str
    provider: str | None
    workspace: str | None
    exit_code: int | None
    error: str | None


class TaskStore:
    def __init__(self, database: Path):
        self.database = Path(database)

    def initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            columns = {r[1] for r in db.execute("PRAGMA table_info(tasks)")}
            for name, kind in (
                ("provider", "TEXT"), ("workspace", "TEXT"),
                ("exit_code", "INTEGER"), ("error", "TEXT")
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {kind}")
            db.execute("""CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                occurred_at TEXT NOT NULL
            )""")

    def get(self, task_id: str) -> Task | None:
        with sqlite3.connect(self.database) as db:
            row = db.execute(
                "SELECT id,status,provider,workspace,exit_code,error FROM tasks WHERE id=?",
                (task_id,)
            ).fetchone()
        return Task(*row) if row else None

    def transition(self, task_id: str, status: str, *,
                   provider: str | None = None, workspace: str | None = None,
                   exit_code: int | None = None, error: str | None = None) -> None:
        timestamp = now()
        with sqlite3.connect(self.database) as db:
            db.execute("""INSERT INTO tasks
                (id,status,updated_at,provider,workspace,exit_code,error)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                status=excluded.status, updated_at=excluded.updated_at,
                provider=COALESCE(excluded.provider,tasks.provider),
                workspace=COALESCE(excluded.workspace,tasks.workspace),
                exit_code=excluded.exit_code,error=excluded.error
            """, (task_id,status,timestamp,provider,workspace,exit_code,error))
            db.execute(
                "INSERT INTO task_events(task_id,status,detail,occurred_at) VALUES (?,?,?,?)",
                (task_id,status,error,timestamp)
            )


class LifecycleEngine:
    def __init__(self, repository: Path, database: Path):
        self.worktrees = WorktreeManager(repository)
        self.store = TaskStore(database)
        self.store.initialize()

    def execute(self, task_id: str, prompt: str, provider: str,
                timeout: int = 600) -> AgentResult:
        if self.store.get(task_id):
            raise ValueError(f"Task {task_id} already exists; refusing to rerun automatically")
        if not prompt.strip():
            raise ValueError("Prompt cannot be empty")
        adapter = CLIProvider(provider)
        self.store.transition(task_id, "queued", provider=provider)
        try:
            workspace = self.worktrees.create(task_id)
            self.store.transition(task_id, "running", workspace=str(workspace.path))
            result = adapter.execute(prompt, workspace.path, timeout)
            if result.exit_code:
                self.store.transition(task_id, "failed", exit_code=result.exit_code,
                                      error=result.error[:2000])
            else:
                self.store.transition(task_id, "awaiting_validation", exit_code=0)
            return result
        except Exception as exc:
            self.store.transition(task_id, "failed", error=str(exc)[:2000])
            raise

    def validate(self, task_id: str, test_command: list[str],
                 security_command: list[str], reviewer: str,
                 timeout: int = 300) -> bool:
        """Run required test, security, and independent review gates in order."""
        task = self.store.get(task_id)
        if task is None or task.status != "awaiting_validation" or not task.workspace:
            raise ValueError("Task must be awaiting_validation")
        workspace = Path(task.workspace)
        if not workspace.is_dir():
            self.store.transition(task_id, "blocked", error="Workspace missing")
            return False
        for label, command in (("tests", test_command), ("security", security_command)):
            self.store.transition(task_id, f"validating_{label}")
            result = command_gate(workspace, command, timeout)
            if not result.passed:
                self.store.transition(task_id, "blocked", error=f"{label}: {result.detail}"[:2000])
                return False
        self.store.transition(task_id, "reviewing")
        try:
            verdict = independent_review(workspace, task.provider or "", reviewer, timeout)
        except Exception as exc:
            self.store.transition(task_id, "blocked", error=f"review: {exc}"[:2000])
            return False
        if not verdict.passed:
            self.store.transition(task_id, "changes_requested", error=verdict.detail[:2000])
            return False
        self.store.transition(task_id, "awaiting_documentation")
        return True


    def document(self, task_id: str, provider: str, timeout: int = 600) -> bool:
        """Run documentation specialist only after quality gates pass."""
        task = self.store.get(task_id)
        if task is None or task.status != "awaiting_documentation" or not task.workspace:
            raise ValueError("Task must be awaiting_documentation")
        workspace = Path(task.workspace)
        if not workspace.is_dir():
            self.store.transition(task_id, "blocked", error="Workspace missing")
            return False
        self.store.transition(task_id, "documenting")
        try:
            result = run_documentation_agent(workspace, provider, timeout)
        except Exception as exc:
            self.store.transition(task_id, "blocked", error=f"documentation: {exc}"[:2000])
            return False
        if not result.passed:
            self.store.transition(task_id, "blocked", error=result.detail[:2000])
            return False
        # Documentation changes can affect code examples; full validation must
        # be rerun before a PR is eligible for merge.
        self.store.transition(task_id, "awaiting_post_docs_validation")
        return True

    def validate_after_docs(self, task_id: str, test_command: list[str],
                            security_command: list[str], reviewer: str,
                            timeout: int = 300) -> bool:
        """Re-run gates after documentation; never mark complete or merge."""
        task = self.store.get(task_id)
        if task is None or task.status != "awaiting_post_docs_validation" or not task.workspace:
            raise ValueError("Task must be awaiting_post_docs_validation")
        workspace = Path(task.workspace)
        if not workspace.is_dir():
            self.store.transition(task_id, "blocked", error="Workspace missing")
            return False
        for label, command in (("post_docs_tests", test_command),
                               ("post_docs_security", security_command)):
            self.store.transition(task_id, f"validating_{label}")
            result = command_gate(workspace, command, timeout)
            if not result.passed:
                self.store.transition(task_id, "blocked", error=f"{label}: {result.detail}"[:2000])
                return False
        self.store.transition(task_id, "post_docs_reviewing")
        try:
            verdict = independent_review(workspace, task.provider or "", reviewer, timeout)
        except Exception as exc:
            self.store.transition(task_id, "blocked", error=f"post_docs_review: {exc}"[:2000])
            return False
        if not verdict.passed:
            self.store.transition(task_id, "changes_requested", error=verdict.detail[:2000])
            return False
        self.store.transition(task_id, "awaiting_pr")
        return True


    def create_pull_request(self, task_id: str, title: str, body: str,
                            base: str = "main") -> str:
        """Publish draft PR only after post-documentation gates pass."""
        task = self.store.get(task_id)
        if task is None or task.status != "awaiting_pr" or not task.workspace:
            raise ValueError("Task must be awaiting_pr")
        branch = self.worktrees._identity(task_id).branch
        try:
            pr = publish_pr(Path(task.workspace), branch, title, body, base=base)
        except Exception as exc:
            self.store.transition(task_id, "awaiting_pr", error=f"PR publishing: {exc}"[:2000])
            raise
        self.store.transition(task_id, "pr_created", error=pr.url)
        return pr.url
