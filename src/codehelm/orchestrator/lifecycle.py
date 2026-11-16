"""Persisted single-task execution lifecycle.

A successful agent exit stops at awaiting_validation. Test, security, review,
documentation and post-documentation gates are explicit later commands.
When `.codehelm/quality.yaml` is present, the durable scheduler can run those
gates itself. Recorded evidence then decides blocked, awaiting_human_approval
or ready_for_pr before a pull request. Acceptance is a test-report line or an
operator attestation. Human approval is the GitHub login recorded by
quality-approve.
"""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from codehelm.execution.worktrees import WorktreeManager
from codehelm.providers.adapters import CLIProvider, AgentResult
from codehelm.quality.gates import GateResult, command_gate, independent_review
from codehelm.quality.contract import (
    assess, decision_error, load_policy, parse_acceptance_report, policy_path_for,
    read_acceptance_report, task_attempt,
)
from codehelm.agents.documentation import run_documentation_agent
from codehelm.integrations.github_pr import publish_pr

# State schema 2 adds quality evidence. Policy schema_version is a separate number.
STATE_SCHEMA_VERSION = 2
_EVIDENCE_SOURCES = {
    "tests": {"command_gate"},
    "security": {"command_gate"},
    "review": {"independent_review"},
    "architecture": {"independent_review"},
    "docs": {"documentation_agent"},
    "acceptance": {"human", "test_report"},
    "acceptance_planned": {"plan"},
    "human_approval": {"human"},
    "oversized_justification": {"human"},
    "finding": {"independent_review", "human"},
    "risk": {"human"},
}


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


class ClaimLost(RuntimeError):
    """The dispatch lease was revoked while a claimed story was still advancing."""


@dataclass(frozen=True)
class EvidenceRow:
    gate: str
    source: str
    passed: bool
    exit_code: int | None
    detail: str
    identity: str
    attempt: int


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
            db.execute("""CREATE TABLE IF NOT EXISTS quality_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1,
                gate TEXT NOT NULL,
                source TEXT NOT NULL,
                passed INTEGER NOT NULL,
                exit_code INTEGER,
                detail TEXT NOT NULL DEFAULT '',
                identity TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS quality_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1,
                story_id TEXT NOT NULL DEFAULT '',
                policy_schema_version INTEGER,
                policy_present INTEGER NOT NULL,
                status TEXT NOT NULL,
                failures_json TEXT NOT NULL,
                requires_human INTEGER NOT NULL,
                evaluated_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )""")
            self._ensure_columns(db, "quality_evidence", (
                ("attempt", "INTEGER NOT NULL DEFAULT 1"),
                ("exit_code", "INTEGER"),
                ("detail", "TEXT NOT NULL DEFAULT ''"),
                ("identity", "TEXT NOT NULL DEFAULT ''"),
            ))
            self._ensure_columns(db, "quality_evaluations", (
                ("attempt", "INTEGER NOT NULL DEFAULT 1"),
                ("story_id", "TEXT NOT NULL DEFAULT ''"),
                ("policy_schema_version", "INTEGER"),
                ("failures_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("requires_human", "INTEGER NOT NULL DEFAULT 0"),
            ))
            db.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(?,?)",
                       (STATE_SCHEMA_VERSION, now()))

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

    @staticmethod
    def _ensure_columns(db, table: str, columns: tuple[tuple[str, str], ...]) -> None:
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns:
            if name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    def record_evidence(self, task_id: str, gate: str, source: str, *,
                        passed: bool, attempt: int = 1, exit_code: int | None = None,
                        detail: str = "", identity: str = "") -> None:
        """Persist one orchestrator-produced evidence row. Unknown sources are rejected."""
        allowed = _EVIDENCE_SOURCES.get(gate)
        if allowed is None or source not in allowed:
            raise ValueError(f"Untrusted evidence source: {gate}/{source}")
        if attempt < 1:
            raise ValueError("Attempt must be positive")
        if gate in {"acceptance", "oversized_justification", "risk"}:
            detail = (detail or "").strip()
        identity = (identity or "").strip()
        if gate == "human_approval" and (not passed or not identity):
            raise ValueError("Human approval requires an identity")
        if gate in {"acceptance", "oversized_justification", "risk"} and not detail:
            raise ValueError(f"{gate} requires detail text")
        if gate in {"tests", "security"} and passed and exit_code != 0:
            raise ValueError(f"{gate} pass requires exit code 0")
        timestamp = now()
        with sqlite3.connect(self.database) as db:
            db.execute("""INSERT INTO quality_evidence
                (task_id,attempt,gate,source,passed,exit_code,detail,identity,recorded_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (task_id, attempt, gate, source, 1 if passed else 0, exit_code,
                 (detail or "")[:4000], identity or "", timestamp))

    def replace_plan_criteria(self, task_id: str, attempt: int, criteria: list[str]) -> None:
        """Store acceptance criteria taken from the validated plan, not from an agent."""
        if attempt < 1 or not criteria or not all(isinstance(item, str) and item.strip() for item in criteria):
            raise ValueError("Planned acceptance criteria must be nonempty strings")
        timestamp = now()
        with sqlite3.connect(self.database) as db:
            db.execute("""DELETE FROM quality_evidence
                WHERE task_id=? AND attempt=? AND gate='acceptance_planned' AND source='plan'""",
                (task_id, attempt))
            for criterion in criteria:
                db.execute("""INSERT INTO quality_evidence
                    (task_id,attempt,gate,source,passed,exit_code,detail,identity,recorded_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (task_id, attempt, "acceptance_planned", "plan", 1, None,
                     criterion.strip()[:4000], "", timestamp))

    def evidence_for(self, task_id: str, attempt: int) -> list[EvidenceRow]:
        with sqlite3.connect(self.database) as db:
            rows = db.execute("""SELECT gate,source,passed,exit_code,detail,identity,attempt
                FROM quality_evidence WHERE task_id=? AND attempt=? ORDER BY id""",
                (task_id, attempt)).fetchall()
        return [EvidenceRow(gate, source, bool(passed), exit_code, detail or "", identity or "", attempt_no)
                for gate, source, passed, exit_code, detail, identity, attempt_no in rows]

    def record_evaluation(self, task_id: str, attempt: int, story_id: str,
                          schema_version: int | None, status: str, failures_json: str,
                          requires_human: bool) -> None:
        if status not in {"blocked", "awaiting_human_approval", "ready_for_pr"}:
            raise ValueError("Invalid quality outcome")
        with sqlite3.connect(self.database) as db:
            db.execute("""INSERT INTO quality_evaluations
                (task_id,attempt,story_id,policy_schema_version,policy_present,status,
                 failures_json,requires_human,evaluated_at)
                VALUES (?,?,?,?,1,?,?,?,?)""",
                (task_id, attempt, story_id, schema_version, status, failures_json,
                 1 if requires_human else 0, now()))

    def latest_evaluation(self, task_id: str, attempt: int) -> dict | None:
        with sqlite3.connect(self.database) as db:
            row = db.execute("""SELECT status,failures_json,requires_human,policy_schema_version
                FROM quality_evaluations WHERE task_id=? AND attempt=?
                ORDER BY id DESC LIMIT 1""", (task_id, attempt)).fetchone()
        if not row:
            return None
        import json
        return {"status": row[0], "failures": json.loads(row[1]),
                "requires_human": bool(row[2]), "policy_schema_version": row[3]}


def _contract_stages(policy) -> list[tuple[str, str]]:
    """Ordered gates. Documentation is followed by a second test/security/review pass."""
    pre: list[tuple[str, str]] = []
    if policy.require_tests:
        pre.append(("tests", "command"))
    if policy.require_security:
        pre.append(("security", "command"))
    if policy.require_review:
        pre.append(("review", "review"))
    stages = list(pre)
    if policy.require_docs:
        stages.append(("docs", "docs"))
        stages.extend(pre)
    if policy.require_architecture:
        stages.append(("architecture", "architecture"))
    return stages


def _stage_match(row: EvidenceRow, kind: str) -> bool:
    if kind == "command":
        return row.source == "command_gate"
    if kind == "review":
        return row.gate == "review" and row.source == "independent_review"
    if kind == "architecture":
        return row.gate == "architecture" and row.source == "independent_review"
    if kind == "docs":
        return row.source == "documentation_agent"
    return False


def _stage_ok(row: EvidenceRow, kind: str, implementer: str) -> bool:
    if kind == "command":
        return bool(row.passed and row.exit_code == 0)
    if kind in {"review", "architecture"}:
        identity = row.identity.strip()
        return bool(row.passed and identity and identity != implementer.strip())
    if kind == "docs":
        return bool(row.passed)
    return False


def _next_stage_row(rows: list[EvidenceRow], cursor: int, gate: str, kind: str,
                    implementer: str) -> int | str | None:
    """Index of the passing row, 'fail' for a completed rejection, or None if missing."""
    for index, row in enumerate(rows):
        if index <= cursor or row.gate != gate or not _stage_match(row, kind):
            continue
        if _stage_ok(row, kind, implementer):
            return index
        return "fail"
    return None


class LifecycleEngine:
    def __init__(self, repository: Path, database: Path):
        self.worktrees = WorktreeManager(repository)
        self.store = TaskStore(database)
        self.store.initialize()

    def _policy(self):
        return load_policy(policy_path_for(self.store.database))

    def _attempt_of(self, task_id: str, attempt: int | None) -> int:
        return task_attempt(task_id) if attempt is None else attempt

    def _run_named_command(self, task_id: str, attempt: int, workspace: Path, gate: str,
                           command: list[str], timeout: int, status: str) -> GateResult:
        report_before = read_acceptance_report(workspace) if gate == "tests" else None
        self.store.transition(task_id, status)
        result = command_gate(workspace, list(command), timeout)
        detail = f"argv={list(command)!r}\n{result.detail}"
        self._record_gate(task_id, attempt, gate, "command_gate",
                          GateResult(result.passed, detail, result.exit_code))
        if gate == "tests":
            self._record_test_acceptance(task_id, attempt, workspace, result, report_before)
        return result

    def _record_test_acceptance(self, task_id: str, attempt: int, workspace: Path,
                                result: GateResult, report_before: str | None) -> None:
        """Record criteria the test process named. A green exit code names none."""
        file_text = ""
        report_after = read_acceptance_report(workspace)
        if report_after is not None and report_after != report_before:
            file_text = report_after
        named = parse_acceptance_report(result.detail)
        for criterion, ok in parse_acceptance_report(file_text).items():
            if criterion in named:
                named[criterion] = named[criterion] and ok
            else:
                named[criterion] = ok
        suite_passed = bool(result.passed and result.exit_code == 0)
        for criterion, ok in named.items():
            self.store.record_evidence(
                task_id, "acceptance", "test_report", passed=bool(suite_passed and ok),
                attempt=attempt, detail=criterion)

    def _record_gate(self, task_id: str, attempt: int, gate: str, source: str,
                     result: GateResult, identity: str = "") -> None:
        self.store.record_evidence(
            task_id, gate, source, passed=result.passed, attempt=attempt,
            exit_code=result.exit_code, detail=result.detail, identity=identity)

    def _decide(self, task_id: str, attempt: int, policy, acceptance_criteria=None):
        task = self.store.get(task_id)
        workspace = Path(task.workspace) if task and task.workspace else None
        return assess(
            self.store, task_id, attempt=attempt, policy=policy, workspace=workspace,
            acceptance_criteria=acceptance_criteria, story_id=task_id,
            implementer=(task.provider or "") if task else "")

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
                 timeout: int = 300, attempt: int | None = None) -> bool:
        """Run required test, security, and independent review gates in order."""
        task = self.store.get(task_id)
        if task is None or task.status != "awaiting_validation" or not task.workspace:
            raise ValueError("Task must be awaiting_validation")
        attempt = self._attempt_of(task_id, attempt)
        workspace = Path(task.workspace)
        if not workspace.is_dir():
            self.store.transition(task_id, "blocked", error="Workspace missing")
            return False
        for gate, command in (("tests", test_command), ("security", security_command)):
            result = self._run_named_command(
                task_id, attempt, workspace, gate, command, timeout, f"validating_{gate}")
            if not result.passed:
                self.store.transition(task_id, "blocked", error=f"{gate}: {result.detail}"[:2000])
                return False
        self.store.transition(task_id, "reviewing")
        try:
            verdict = independent_review(workspace, task.provider or "", reviewer, timeout)
        except Exception as exc:
            self.store.record_evidence(
                task_id, "review", "independent_review", passed=False, attempt=attempt,
                detail=str(exc), identity=reviewer)
            self.store.transition(task_id, "blocked", error=f"review: {exc}"[:2000])
            return False
        self._record_gate(task_id, attempt, "review", "independent_review", verdict, identity=reviewer)
        if not verdict.passed:
            self.store.transition(task_id, "changes_requested", error=verdict.detail[:2000])
            return False
        self.store.transition(task_id, "awaiting_documentation")
        return True


    def document(self, task_id: str, provider: str, timeout: int = 600,
                 attempt: int | None = None) -> bool:
        """Run documentation specialist only after quality gates pass."""
        task = self.store.get(task_id)
        if task is None or task.status != "awaiting_documentation" or not task.workspace:
            raise ValueError("Task must be awaiting_documentation")
        attempt = self._attempt_of(task_id, attempt)
        workspace = Path(task.workspace)
        if not workspace.is_dir():
            self.store.transition(task_id, "blocked", error="Workspace missing")
            return False
        self.store.transition(task_id, "documenting")
        try:
            result = run_documentation_agent(workspace, provider, timeout)
        except Exception as exc:
            self.store.record_evidence(
                task_id, "docs", "documentation_agent", passed=False, attempt=attempt,
                detail=str(exc))
            self.store.transition(task_id, "blocked", error=f"documentation: {exc}"[:2000])
            return False
        self.store.record_evidence(
            task_id, "docs", "documentation_agent", passed=result.passed, attempt=attempt,
            detail=result.detail)
        if not result.passed:
            self.store.transition(task_id, "blocked", error=result.detail[:2000])
            return False
        # Documentation changes can affect code examples; full validation must
        # be rerun before a PR is eligible for merge.
        self.store.transition(task_id, "awaiting_post_docs_validation")
        return True

    def validate_after_docs(self, task_id: str, test_command: list[str],
                            security_command: list[str], reviewer: str,
                            timeout: int = 300, attempt: int | None = None) -> bool:
        """Re-run gates after documentation; never mark complete or merge."""
        task = self.store.get(task_id)
        if task is None or task.status != "awaiting_post_docs_validation" or not task.workspace:
            raise ValueError("Task must be awaiting_post_docs_validation")
        attempt = self._attempt_of(task_id, attempt)
        workspace = Path(task.workspace)
        if not workspace.is_dir():
            self.store.transition(task_id, "blocked", error="Workspace missing")
            return False
        for gate, label, command in (("tests", "post_docs_tests", test_command),
                                     ("security", "post_docs_security", security_command)):
            result = self._run_named_command(
                task_id, attempt, workspace, gate, command, timeout, f"validating_{label}")
            if not result.passed:
                self.store.transition(task_id, "blocked", error=f"{label}: {result.detail}"[:2000])
                return False
        self.store.transition(task_id, "post_docs_reviewing")
        try:
            verdict = independent_review(workspace, task.provider or "", reviewer, timeout)
        except Exception as exc:
            self.store.record_evidence(
                task_id, "review", "independent_review", passed=False, attempt=attempt,
                detail=str(exc), identity=reviewer)
            self.store.transition(task_id, "blocked", error=f"post_docs_review: {exc}"[:2000])
            return False
        self._record_gate(task_id, attempt, "review", "independent_review", verdict, identity=reviewer)
        policy = self._policy()
        if not verdict.passed and not policy.present:
            self.store.transition(task_id, "changes_requested", error=verdict.detail[:2000])
            return False
        if policy.present and policy.require_architecture and verdict.passed:
            self.store.transition(task_id, "reviewing_architecture")
            try:
                architecture = independent_review(
                    workspace, task.provider or "", reviewer, timeout, kind="architecture")
            except Exception as exc:
                self.store.record_evidence(
                    task_id, "architecture", "independent_review", passed=False, attempt=attempt,
                    detail=str(exc), identity=reviewer)
                self.store.transition(task_id, "blocked", error=f"architecture: {exc}"[:2000])
                return False
            self._record_gate(task_id, attempt, "architecture", "independent_review",
                              architecture, identity=reviewer)
        if not policy.present:
            self.store.transition(task_id, "awaiting_pr")
            return True
        decision = self._decide(task_id, attempt, policy)
        self.store.transition(task_id, decision.status, error=decision_error(decision) or None)
        return decision.status == "ready_for_pr"

    def run_contract_gates(self, task_id: str, test_command: list[str],
                           security_command: list[str], reviewer: str,
                           docs_provider: str, *, timeout: int = 300,
                           docs_timeout: int = 600,
                           heartbeat: Callable[[], bool] | None = None,
                           acceptance_criteria: list[str] | None = None,
                           story_id: str | None = None):
        """Run missing quality stages for one attempt, then score recorded evidence.

        Stages that already have a passing row for this attempt are not repeated.
        A failing row fails closed and does not launch later model calls.
        """
        policy = self._policy()
        if not policy.present:
            raise ValueError("Quality policy is not active")
        task = self.store.get(task_id)
        if task is None or not task.workspace:
            raise ValueError("Task workspace missing")
        if task.status in {"queued", "running", "failed", "pr_created"}:
            raise ValueError("Coding agent has not finished")
        attempt = self._attempt_of(task_id, None)
        implementer = task.provider or ""
        stages = _contract_stages(policy)
        if any(kind in {"review", "architecture"} for _, kind in stages):
            if not reviewer or reviewer == implementer:
                raise ValueError("Quality policy requires a reviewer that is not the implementer")
        workspace = Path(task.workspace)
        if not workspace.is_dir():
            self._beat(heartbeat)
            decision = self._score_contract(
                task_id, attempt, policy, None, acceptance_criteria, story_id, implementer)
            return decision

        cursor = -1
        pending = 0
        docs_index = next((index for index, stage in enumerate(stages) if stage[1] == "docs"), None)
        while pending < len(stages):
            self._beat(heartbeat)
            gate, kind = stages[pending]
            rows = self.store.evidence_for(task_id, attempt)
            outcome = _next_stage_row(rows, cursor, gate, kind, implementer)
            if outcome == "fail":
                return self._score_contract(
                    task_id, attempt, policy, workspace, acceptance_criteria, story_id, implementer)
            if isinstance(outcome, int):
                cursor = outcome
                pending += 1
                continue
            post = docs_index is not None and pending > docs_index
            before = len(rows)
            self._execute_contract_stage(
                task_id, attempt, workspace, gate, kind, post=post,
                test_command=test_command, security_command=security_command,
                reviewer=reviewer, docs_provider=docs_provider or implementer,
                timeout=timeout, docs_timeout=docs_timeout, implementer=implementer)
            if len(self.store.evidence_for(task_id, attempt)) <= before:
                raise RuntimeError(f"Quality stage {gate} did not record evidence")
        self._beat(heartbeat)
        return self._score_contract(
            task_id, attempt, policy, workspace, acceptance_criteria, story_id, implementer)

    @staticmethod
    def _beat(heartbeat: Callable[[], bool] | None) -> None:
        if heartbeat is not None and not heartbeat():
            raise ClaimLost("Claim lost during quality gates")

    def _score_contract(self, task_id: str, attempt: int, policy, workspace: Path | None,
                        acceptance_criteria, story_id: str | None, implementer: str):
        decision = assess(
            self.store, task_id, attempt=attempt, policy=policy, workspace=workspace,
            acceptance_criteria=acceptance_criteria, story_id=story_id or task_id,
            implementer=implementer)
        self.store.transition(task_id, decision.status, error=decision_error(decision) or None)
        return decision

    def _execute_contract_stage(self, task_id: str, attempt: int, workspace: Path,
                                gate: str, kind: str, *, post: bool,
                                test_command: list[str], security_command: list[str],
                                reviewer: str, docs_provider: str, timeout: int,
                                docs_timeout: int, implementer: str) -> None:
        if kind == "command":
            label = f"post_docs_{gate}" if post else gate
            command = list(test_command if gate == "tests" else security_command)
            self._run_named_command(
                task_id, attempt, workspace, gate, command, timeout, f"validating_{label}")
            return
        if kind == "docs":
            self.store.transition(task_id, "documenting")
            try:
                result = run_documentation_agent(workspace, docs_provider, docs_timeout)
            except Exception as exc:
                self.store.record_evidence(
                    task_id, "docs", "documentation_agent", passed=False, attempt=attempt,
                    detail=str(exc)[:4000])
                return
            self.store.record_evidence(
                task_id, "docs", "documentation_agent", passed=result.passed, attempt=attempt,
                detail=result.detail)
            return
        review_kind = "architecture" if kind == "architecture" else "code"
        gate_name = "architecture" if kind == "architecture" else "review"
        if kind == "architecture":
            status = "reviewing_architecture"
        elif post:
            status = "post_docs_reviewing"
        else:
            status = "reviewing"
        self.store.transition(task_id, status)
        try:
            verdict = independent_review(
                workspace, implementer, reviewer, timeout, kind=review_kind)
        except Exception as exc:
            self.store.record_evidence(
                task_id, gate_name, "independent_review", passed=False, attempt=attempt,
                detail=str(exc)[:4000], identity=reviewer)
            return
        self._record_gate(task_id, attempt, gate_name, "independent_review", verdict,
                          identity=reviewer)

    def create_pull_request(self, task_id: str, title: str, body: str,
                            base: str = "main") -> str:
        """Publish draft PR only after post-documentation gates pass."""
        task = self.store.get(task_id)
        policy = self._policy()
        required = "ready_for_pr" if policy.present else "awaiting_pr"
        if task is None or task.status != required or not task.workspace:
            raise ValueError(f"Task must be {required}")
        if policy.present:
            decision = self._decide(task_id, task_attempt(task_id), policy)
            if decision.status != "ready_for_pr":
                self.store.transition(task_id, decision.status, error=decision_error(decision) or None)
                raise ValueError(f"Quality contract status is {decision.status}")
        branch = self.worktrees._identity(task_id).branch
        try:
            pr = publish_pr(Path(task.workspace), branch, title, body, base=base)
        except Exception as exc:
            # Keep the status pr-create will accept on retry. A policy-gated task
            # must stay ready_for_pr; awaiting_pr would make the next attempt fail closed.
            self.store.transition(task_id, required, error=f"PR publishing: {exc}"[:2000])
            raise
        self.store.transition(task_id, "pr_created", error=pr.url)
        return pr.url
