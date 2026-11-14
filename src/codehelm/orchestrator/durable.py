"""SQLite-backed dispatch claims, bounded retries and conservative recovery."""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sqlite3

from codehelm.orchestrator.lifecycle import TaskStore
from codehelm.planning.planner import Plan


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
                if monthly_budget is not None and (
                    monthly_used + aud_amount > monthly_budget.cap_aud or
                    epic_used + aud_amount > monthly_budget.epic_cap_aud):
                    break
                attempt = rows[story.id][1] + 1
                task_id = f"{self.prefix}-{story.id}-A{attempt}"
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
        with sqlite3.connect(self.store.database) as db:
            result = db.execute(
                """UPDATE dispatches SET state=?, error=? WHERE prefix=? AND story_id=?
                   AND state='claimed'""",
                (status, error[:1000], self.prefix, story_id))
            if result.rowcount != 1:
                raise ValueError("Story is not currently claimed")

    def retry(self, story_id: str, budget: DispatchBudget) -> None:
        """Explicit retry authorization after inspecting the previous attempt."""
        with sqlite3.connect(self.store.database) as db:
            row = db.execute("SELECT state,attempts FROM dispatches WHERE prefix=? AND story_id=?",
                             (self.prefix, story_id)).fetchone()
            if not row or row[0] not in {"failed", "interrupted"}:
                raise ValueError("Only failed or interrupted stories can be retried")
            if row[1] >= budget.max_attempts:
                raise ValueError("Per-story attempt limit reached")
            db.execute("UPDATE dispatches SET state='pending',error=NULL WHERE prefix=? AND story_id=?",
                       (self.prefix, story_id))

    def complete(self, story_id: str, *, human_verified_merge: bool = False) -> None:
        """Manual completion requires explicit external verification; no API merge check."""
        attempt = self.snapshot().get(story_id, ("", 0))[1]
        if not human_verified_merge:
            raise ValueError("Human verification of merged PR is required")
        task = self.store.get(f"{self.prefix}-{story_id}-A{attempt}")
        if not task or task.status != "pr_created":
            raise ValueError("A draft PR must exist before manual completion")
        with sqlite3.connect(self.store.database) as db:
            result = db.execute("""UPDATE dispatches SET state='completed'
                                   WHERE prefix=? AND story_id=? AND state='awaiting_validation'""",
                                (self.prefix, story_id))
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
