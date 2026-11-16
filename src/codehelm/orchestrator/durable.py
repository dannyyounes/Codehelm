"""SQLite-backed dispatch claims, bounded retries and conservative recovery.

When a quality policy file exists, a claimed story can continue through the
quality gates before the claim is released. A missing policy leaves a
successful agent at awaiting_validation.
"""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
import re
import sqlite3

from codehelm.orchestrator.lifecycle import ClaimLost, LifecycleEngine, TaskStore
from codehelm.planning.planner import Plan
from codehelm.integrations.github_pr import PullRequestError, github_pull_request_merged
from codehelm.quality.contract import assess, decision_error, load_policy, policy_path_for

# A live or unfinished coding agent must not be relaunched on resume.
_NOT_RESUMABLE = frozenset({"queued", "running", "failed", "pr_created"})
_DURABLE_RESULTS = frozenset({
    "awaiting_validation", "failed", "blocked", "awaiting_human_approval", "ready_for_pr",
})
# PREFIX-STORY-An, for example PLAN-S1-A1. The story id is the middle segment.
_RESUME_TASK_ID = re.compile(
    r"^([A-Za-z0-9_]+)-([A-Za-z0-9_]+)-A([1-9][0-9]*)$"
)


def parse_resume_task_id(task_id: str) -> tuple[str, str, int]:
    """Return prefix, story id, and attempt. Reject any other shape."""
    match = _RESUME_TASK_ID.fullmatch(task_id or "")
    if not match:
        raise ValueError("Task id must look like PREFIX-STORY-An, for example PLAN-S1-A1")
    return match.group(1), match.group(2), int(match.group(3))


@dataclass(frozen=True)
class DispatchBudget:
    max_attempts: int = 2
    max_total_attempts: int = 20
    max_active: int = 2
    lease_seconds: int = 900

    def __post_init__(self):
        if not (1 <= self.max_attempts <= 5 and 1 <= self.max_total_attempts <= 100
                and 1 <= self.max_active <= 8 and 60 <= self.lease_seconds <= 86400):
            raise ValueError("Invalid dispatch budget")


def resolve_max_attempts(policy, requested: int | None) -> int:
    if requested is not None:
        return requested
    if getattr(policy, "present", False):
        return policy.max_retries
    return 2


class DurableScheduler:
    def __init__(self, database: Path, plan: Plan, prefix: str):
        self.store = TaskStore(database)
        self.store.initialize()
        self.plan = plan
        self.prefix = prefix
        if not prefix or not prefix.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Invalid prefix")
        with sqlite3.connect(self.store.database) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS dispatches (
                prefix TEXT NOT NULL, story_id TEXT NOT NULL,
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                claimed_at TEXT, error TEXT,
                PRIMARY KEY(prefix, story_id)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS scheduler_checkpoints(
                prefix TEXT NOT NULL, created_at TEXT NOT NULL,
                state_json TEXT NOT NULL)""")
            for story in plan.stories:
                db.execute("""INSERT OR IGNORE INTO dispatches(prefix,story_id,state)
                              VALUES(?,?,'pending')""", (prefix, story.id))
            existing = {row[0] for row in db.execute(
                "SELECT story_id FROM dispatches WHERE prefix=?", (prefix,))}
            if existing != {s.id for s in plan.stories}:
                raise ValueError("Plan changed for an existing prefix; use a new prefix")

    def snapshot(self) -> dict[str, tuple[str, int]]:
        with sqlite3.connect(self.store.database) as db:
            return {sid: (state, attempts) for sid, state, attempts in db.execute(
                "SELECT story_id,state,attempts FROM dispatches WHERE prefix=?", (self.prefix,))}

    def recover_expired(self, budget: DispatchBudget, now: datetime | None = None) -> int:
        """Mark expired claims interrupted; NEVER blindly relaunch a possibly running agent."""
        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(seconds=budget.lease_seconds)).isoformat()
        with sqlite3.connect(self.store.database, timeout=30) as db:
            result = db.execute(
                """UPDATE dispatches SET state='interrupted', error='Lease expired; inspect process/worktree before retry'
                   WHERE prefix=? AND state='claimed' AND claimed_at < ?""",
                (self.prefix, cutoff))
            return result.rowcount

    def claim(self, budget: DispatchBudget) -> list[str]:
        """Atomic SQLite transaction prevents two scheduler processes double-claiming."""
        with sqlite3.connect(self.store.database, timeout=30, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = {sid: (state, attempts) for sid, state, attempts in db.execute(
                "SELECT story_id,state,attempts FROM dispatches WHERE prefix=?", (self.prefix,))}
            active = sum(state == "claimed" for state, _ in rows.values())
            used = sum(attempts for _, attempts in rows.values())
            capacity = min(budget.max_active - active, budget.max_total_attempts - used)
            if capacity <= 0:
                db.execute("COMMIT")
                return []
            completed = {sid for sid, (state, _) in rows.items() if state == "completed"}
            ready = [s.id for s in self.plan.stories
                     if rows[s.id][0] == "pending"
                     and rows[s.id][1] < budget.max_attempts
                     and set(s.dependencies) <= completed]
            chosen = ready[:capacity]
            timestamp = datetime.now(timezone.utc).isoformat()
            for sid in chosen:
                db.execute("""UPDATE dispatches SET state='claimed', attempts=attempts+1,
                              claimed_at=?, error=NULL WHERE prefix=? AND story_id=?""",
                           (timestamp, self.prefix, sid))
            db.execute("COMMIT")
            return chosen

    def claim_budgeted(self, budget: DispatchBudget, provider: str,
                       reserve_tokens: int, reserve_usd, limits,
                       monthly_budget=None, aud_per_task=None, month=None) -> list[str]:
        """Atomically claim eligible stories AND reserve their estimated budget."""
        from decimal import Decimal
        from codehelm.orchestrator.budgets import BudgetLedger
        amount = Decimal(str(reserve_usd))
        if reserve_tokens < 1 or not amount.is_finite() or amount <= 0:
            raise ValueError("Reservation must be positive")
        ledger = BudgetLedger(self.store.database)
        if monthly_budget is not None:
            from decimal import Decimal
            if aud_per_task is None or not Decimal(str(aud_per_task)).is_finite() or Decimal(str(aud_per_task)) < 0:
                raise ValueError("Invalid AUD reservation")
            # Create table before acquiring the scheduler's write transaction.
            from codehelm.orchestrator.monthly_budget import MonthlyBudget
            MonthlyBudget(self.store.database, monthly_budget)
        with sqlite3.connect(self.store.database, timeout=30, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = {sid: (state, attempts) for sid, state, attempts in db.execute(
                "SELECT story_id,state,attempts FROM dispatches WHERE prefix=?", (self.prefix,))}
            active = sum(state == "claimed" for state, _ in rows.values())
            used = sum(attempts for _, attempts in rows.values())
            capacity = min(budget.max_active - active, budget.max_total_attempts - used)
            completed = {sid for sid, (state, _) in rows.items() if state == "completed"}
            ready = [story for story in self.plan.stories
                     if rows[story.id][0] == "pending"
                     and rows[story.id][1] < budget.max_attempts
                     and set(story.dependencies) <= completed]
            usage = db.execute("""SELECT reserved_tokens,reserved_usd,actual_tokens,actual_usd
                                  FROM budget_reservations WHERE prefix=?""", (self.prefix,)).fetchall()
            tokens_used = sum(actual if actual is not None else reserved
                              for reserved, _, actual, _ in usage)
            dollars_used = sum((Decimal(actual) if actual is not None else Decimal(reserved))
                               for _, reserved, _, actual in usage)
            selected = []
            if monthly_budget is not None:
                from decimal import Decimal
                month = month or datetime.now(timezone.utc).strftime("%Y-%m")
                aud_rows = db.execute("""SELECT epic,estimated_aud,actual_aud FROM aud_reservations
                    WHERE month=? AND charge_type='additional'""", (month,)).fetchall()
                monthly_used = sum((Decimal(actual if actual is not None else estimated)
                                    for _, estimated, actual in aud_rows), Decimal("0"))
                epic_used = sum((Decimal(actual if actual is not None else estimated)
                                 for epic, estimated, actual in aud_rows
                                 if epic == self.prefix), Decimal("0"))
                aud_amount = Decimal(str(aud_per_task))
            now = datetime.now(timezone.utc).isoformat()
            for story in ready[:max(0, capacity)]:
                if tokens_used + reserve_tokens > limits.tokens or dollars_used + amount > limits.usd:
                    break
                attempt = rows[story.id][1] + 1
                task_id = f"{self.prefix}-{story.id}-A{attempt}"
                override_id = None
                if monthly_budget is not None and (
                    monthly_used + aud_amount > monthly_budget.cap_aud or
                    epic_used + aud_amount > monthly_budget.epic_cap_aud):
                    from codehelm.orchestrator.monthly_budget import _matching_override
                    override_id = _matching_override(
                        db, task_id, self.prefix, month, aud_amount)
                    if override_id is None:
                        break
                db.execute("""INSERT INTO budget_reservations
                    (prefix,task_id,provider,reserved_tokens,reserved_usd,status,created_at)
                    VALUES (?,?,?,?,?,'reserved',?)""",
                    (self.prefix, task_id, provider, reserve_tokens, str(amount), now))
                db.execute("""UPDATE dispatches SET state='claimed',attempts=?,
                              claimed_at=?,error=NULL WHERE prefix=? AND story_id=?""",
                           (attempt, now, self.prefix, story.id))
                tokens_used += reserve_tokens
                dollars_used += amount
                if monthly_budget is not None:
                    db.execute("""INSERT INTO aud_reservations
                        (task_id,epic,month,estimated_aud,charge_type,created_at)
                        VALUES (?,?,?,?,'additional',?)""",
                        (task_id,self.prefix,month,str(aud_amount),now))
                    if override_id is not None:
                        updated = db.execute(
                            """UPDATE budget_overrides SET used_at=?
                               WHERE id=? AND used_at IS NULL""",
                            (now, override_id))
                        if updated.rowcount != 1:
                            raise ValueError("Budget approval required; new task paused")
                    monthly_used += aud_amount
                    epic_used += aud_amount
                selected.append(story.id)
            db.execute("COMMIT")
            return selected

    def heartbeat(self, story_id: str) -> bool:
        """Refresh a live claim; false means the claim was revoked or interrupted."""
        with sqlite3.connect(self.store.database, timeout=30) as db:
            result = db.execute("""UPDATE dispatches SET claimed_at=?
                WHERE prefix=? AND story_id=? AND state='claimed'""",
                (datetime.now(timezone.utc).isoformat(), self.prefix, story_id))
            return result.rowcount == 1

    def finish(self, story_id: str, status: str, error: str = "") -> None:
        if status not in {"awaiting_validation", "failed"}:
            raise ValueError("Invalid agent result")
        self._release_claim(story_id, status, error)

    def settle(self, story_id: str, status: str, error: str = "") -> None:
        """Release a live claim to an agent result or a quality outcome."""
        if status not in _DURABLE_RESULTS:
            raise ValueError("Invalid durable result")
        self._release_claim(story_id, status, error)

    def _release_claim(self, story_id: str, status: str, error: str) -> None:
        with sqlite3.connect(self.store.database) as db:
            result = db.execute(
                """UPDATE dispatches SET state=?, error=? WHERE prefix=? AND story_id=?
                   AND state='claimed'""",
                (status, (error or "")[:1000], self.prefix, story_id))
            if result.rowcount != 1:
                raise ValueError("Story is not currently claimed")

    def claim_resumable(self, budget: DispatchBudget) -> list[str]:
        """Re-claim interrupted stories whose coding agent already finished.

        Attempts and budget reservations are left as they were. Stories still
        queued, running, or failed stay interrupted so a live agent is not relaunched.
        A missing policy file resumes nothing.
        """
        policy = load_policy(policy_path_for(self.store.database))
        if not policy.present:
            return []
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.store.database, timeout=30, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = {sid: (state, attempts) for sid, state, attempts in db.execute(
                "SELECT story_id,state,attempts FROM dispatches WHERE prefix=?", (self.prefix,))}
            active = sum(state == "claimed" for state, _ in rows.values())
            capacity = budget.max_active - active
            selected: list[str] = []
            if capacity > 0:
                for story in self.plan.stories:
                    if len(selected) >= capacity:
                        break
                    state, attempts = rows.get(story.id, ("", 0))
                    if state != "interrupted" or attempts < 1:
                        continue
                    task_id = f"{self.prefix}-{story.id}-A{attempts}"
                    task = db.execute(
                        "SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
                    if task is None or task[0] in _NOT_RESUMABLE:
                        continue
                    result = db.execute(
                        """UPDATE dispatches SET state='claimed', claimed_at=?, error=NULL
                           WHERE prefix=? AND story_id=? AND state='interrupted'""",
                        (now, self.prefix, story.id))
                    if result.rowcount == 1:
                        selected.append(story.id)
            db.execute("COMMIT")
            return selected

    def reclaim_interrupted(self, story_id: str) -> None:
        """Re-claim one interrupted story on the same attempt.

        Attempts stay as they are. The task row for that attempt must exist and
        must not be queued, running, failed, or pr_created. A missing quality
        policy does not block this single-story reclaim. Other stories are left
        untouched, and a story that is not interrupted is not moved.
        """
        if not any(story.id == story_id for story in self.plan.stories):
            raise ValueError("Unknown story")
        timestamp = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.store.database, timeout=30, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state, attempts FROM dispatches WHERE prefix=? AND story_id=?",
                (self.prefix, story_id)).fetchone()
            if row is None or row[0] != "interrupted" or row[1] < 1:
                raise ValueError("Story is not interrupted")
            task_id = f"{self.prefix}-{story_id}-A{row[1]}"
            task = db.execute(
                "SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None or task[0] in _NOT_RESUMABLE:
                raise ValueError("Task is not resumable")
            result = db.execute(
                """UPDATE dispatches SET state='claimed', claimed_at=?, error=NULL
                   WHERE prefix=? AND story_id=? AND state='interrupted'""",
                (timestamp, self.prefix, story_id))
            if result.rowcount != 1:
                raise ValueError("Story is not interrupted")
            db.execute("COMMIT")

    def carry_story(self, story_id: str, engine: LifecycleEngine, *, prompt: str, provider: str,
                    test_command: list[str] | None = None, security_command: list[str] | None = None,
                    reviewer: str = "", docs_provider: str = "",
                    heartbeat: Callable[[], bool] | None = None,
                    timeout: int = 300, agent_timeout: int = 600) -> str:
        """Advance one claimed story. Quality gates run only when a policy file exists.

        The coding agent is started only when this attempt has no task row yet.
        Gate commands and model reviews that already passed for the attempt are
        not repeated. A green test exit code does not verify acceptance criteria.
        Only a CODEHELM_ACCEPTANCE line from that test process does.
        """
        snapshot = self.snapshot()
        if story_id not in snapshot:
            raise ValueError("Unknown story")
        state, attempts = snapshot[story_id]
        if state != "claimed":
            raise ValueError("Story is not currently claimed")
        if attempts < 1:
            raise ValueError("No attempt to advance")
        story = next((item for item in self.plan.stories if item.id == story_id), None)
        if story is None:
            raise ValueError("Unknown story")
        policy = load_policy(policy_path_for(self.store.database))
        needs_reviewer = policy.present and (policy.require_review or policy.require_architecture)
        if needs_reviewer and (not reviewer or reviewer == provider):
            raise ValueError("Quality policy requires a reviewer that is not the implementer")
        task_id = f"{self.prefix}-{story_id}-A{attempts}"
        task = self.store.get(task_id)
        if task is None:
            if heartbeat is not None and not heartbeat():
                raise ClaimLost("Claim lost before the coding agent started")
            result = engine.execute(task_id, prompt, provider, agent_timeout)
            if result.exit_code:
                self.settle(story_id, "failed", result.error)
                return "failed"
        elif task.status in {"queued", "running"}:
            raise ValueError("Coding agent still in progress; refusing to relaunch")
        elif task.status == "failed":
            self.settle(story_id, "failed", task.error or "agent failed")
            return "failed"
        if not policy.present:
            self.settle(story_id, "awaiting_validation")
            return "awaiting_validation"
        decision = engine.run_contract_gates(
            task_id, list(test_command or []), list(security_command or []), reviewer,
            docs_provider or provider, timeout=timeout, docs_timeout=agent_timeout,
            heartbeat=heartbeat, acceptance_criteria=list(story.acceptance_criteria),
            story_id=story.id)
        self.settle(story_id, decision.status, decision_error(decision))
        return decision.status

    def retry(self, story_id: str, budget: DispatchBudget) -> None:
        """Explicit retry authorization after inspecting the previous attempt."""
        with sqlite3.connect(self.store.database) as db:
            row = db.execute("SELECT state,attempts FROM dispatches WHERE prefix=? AND story_id=?",
                             (self.prefix, story_id)).fetchone()
            if not row or row[0] not in {"failed", "interrupted", "blocked"}:
                raise ValueError("Only failed, interrupted, or quality-blocked stories can be retried")
            if row[1] >= budget.max_attempts:
                raise ValueError("Per-story attempt limit reached")
            db.execute("UPDATE dispatches SET state='pending',error=NULL WHERE prefix=? AND story_id=?",
                       (self.prefix, story_id))

    def apply_quality(self, story_id: str) -> str:
        """Move a story to blocked, awaiting_human_approval, or ready_for_pr from recorded evidence.

        A missing policy file does not change dispatch state. Interrupted stories are
        refused so a partial attempt cannot advance. Evidence from older attempts is ignored.
        """
        policy = load_policy(policy_path_for(self.store.database))
        snapshot = self.snapshot()
        if story_id not in snapshot:
            raise ValueError("Unknown story")
        state, attempts = snapshot[story_id]
        if state == "interrupted":
            raise ValueError("Story is interrupted; inspect and explicitly retry before quality evaluation")
        if not policy.present:
            return state
        if state not in {"awaiting_validation", "blocked", "awaiting_human_approval", "ready_for_pr"}:
            raise ValueError("Story is not awaiting quality evaluation")
        if attempts < 1:
            raise ValueError("No attempt to evaluate")
        story = next((item for item in self.plan.stories if item.id == story_id), None)
        if story is None:
            raise ValueError("Unknown story")
        task_id = f"{self.prefix}-{story_id}-A{attempts}"
        task = self.store.get(task_id)
        workspace = Path(task.workspace) if task and task.workspace else None
        decision = assess(
            self.store, task_id, attempt=attempts, policy=policy, workspace=workspace,
            acceptance_criteria=story.acceptance_criteria, story_id=story.id,
            implementer=(task.provider or "") if task else "")
        error = decision_error(decision)
        with sqlite3.connect(self.store.database, timeout=30) as db:
            result = db.execute("""UPDATE dispatches SET state=?, error=?
                WHERE prefix=? AND story_id=? AND state=?""",
                (decision.status, error[:1000], self.prefix, story_id, state))
            if result.rowcount != 1:
                raise ValueError("Story state changed during evaluation")
        if task:
            self.store.transition(task_id, decision.status, error=error or None)
        return decision.status

    def complete(self, story_id: str, *, human_verified_merge: bool = False) -> None:
        """Complete a story only after gh reports the recorded pull request merged."""
        attempt = self.snapshot().get(story_id, ("", 0))[1]
        if not human_verified_merge:
            raise ValueError("Human verification of merged PR is required")
        task_id = f"{self.prefix}-{story_id}-A{attempt}"
        task = self.store.get(task_id)
        if not task or task.status != "pr_created":
            raise ValueError("A draft PR must exist before manual completion")
        policy = load_policy(policy_path_for(self.store.database))
        expected = "awaiting_validation"
        if policy.present:
            latest = self.store.latest_evaluation(task_id, attempt)
            if not latest or latest["status"] != "ready_for_pr":
                raise ValueError("Quality contract must be ready_for_pr before completion")
            expected = "ready_for_pr"
        try:
            github_pull_request_merged(task.error or "")
        except PullRequestError as exc:
            raise ValueError(str(exc)) from exc
        with sqlite3.connect(self.store.database) as db:
            result = db.execute("""UPDATE dispatches SET state='completed'
                                   WHERE prefix=? AND story_id=? AND state=?""",
                                (self.prefix, story_id, expected))
            if result.rowcount != 1:
                raise ValueError("Story has not finished its dispatch")

    def checkpoint(self) -> dict:
        """Persist a durable state snapshot; does not resume live CLI sessions."""
        import json
        state = self.snapshot()
        with sqlite3.connect(self.store.database) as db:
            db.execute("""INSERT INTO scheduler_checkpoints(prefix,created_at,state_json)
                          VALUES(?,?,?)""", (self.prefix,
                          datetime.now(timezone.utc).isoformat(), json.dumps(state)))
        return state
