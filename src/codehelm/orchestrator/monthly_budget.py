"""Monthly AUD additional-charge budget, separate from subscription usage.

Admission reservations are estimates; not provider-side hard spending caps.
A one-shot override admits one exact additional charge and does not raise the cap.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class MonthlyPolicy:
    warning_aud: Decimal = Decimal("10")
    critical_aud: Decimal = Decimal("15")
    cap_aud: Decimal = Decimal("20")
    epic_cap_aud: Decimal = Decimal("5")
    max_parallel: int = 2


def parse_override_amount(text: str) -> Decimal:
    """Positive finite Decimal. Non-numeric, zero, and negative values are rejected."""
    if not isinstance(text, str):
        raise ValueError("Invalid budget override amount")
    try:
        amount = Decimal(text.strip())
    except (ArithmeticError, ValueError) as exc:
        raise ValueError("Invalid budget override amount") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Invalid budget override amount")
    return amount


class MonthlyBudget:
    def __init__(self, database: Path, policy: MonthlyPolicy = MonthlyPolicy()):
        self.database = Path(database)
        self.policy = policy
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS aud_reservations (
                task_id TEXT PRIMARY KEY, epic TEXT NOT NULL, month TEXT NOT NULL,
                estimated_aud TEXT NOT NULL, actual_aud TEXT,
                charge_type TEXT NOT NULL CHECK(charge_type IN ('additional','subscription')),
                created_at TEXT NOT NULL, rate_aud_per_usd TEXT, rate_source TEXT)""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(aud_reservations)")}
            for name in ("rate_aud_per_usd", "rate_source"):
                if name not in columns:
                    db.execute(f"ALTER TABLE aud_reservations ADD COLUMN {name} TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS budget_overrides (
                id INTEGER PRIMARY KEY,
                task_id TEXT NOT NULL, epic TEXT NOT NULL, month TEXT NOT NULL,
                amount TEXT NOT NULL, approver TEXT NOT NULL, reason TEXT NOT NULL,
                created_at TEXT NOT NULL, used_at TEXT)""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS budget_overrides_one_unused
                ON budget_overrides(task_id) WHERE used_at IS NULL""")

    def _month(self, month: str | None) -> str:
        return month or datetime.now(timezone.utc).strftime("%Y-%m")

    def _rollback(self, db: sqlite3.Connection) -> None:
        if db.in_transaction:
            db.execute("ROLLBACK")

    def status(self, month: str | None = None) -> dict:
        month = self._month(month)
        with sqlite3.connect(self.database) as db:
            rows = db.execute("""SELECT estimated_aud,actual_aud FROM aud_reservations
                                 WHERE month=? AND charge_type='additional'""", (month,)).fetchall()
        total = sum((Decimal(actual if actual is not None else estimated)
                     for estimated, actual in rows), Decimal("0"))
        level = ("paused" if total >= self.policy.cap_aud else
                 "critical" if total >= self.policy.critical_aud else
                 "warning" if total >= self.policy.warning_aud else "normal")
        return {"month": month, "accounted_aud": str(total), "level": level,
                "remaining_aud": str(max(Decimal("0"), self.policy.cap_aud-total))}

    def record_override(self, task_id: str, epic: str, amount: Decimal, approver: str,
                        reason: str, *, month: str | None = None) -> None:
        """Store one unused override. A second unused row for the same task is rejected."""
        month = self._month(month)
        approver = (approver or "").strip()
        reason = (reason or "").strip()
        if (not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0
                or not isinstance(task_id, str) or not task_id.strip()
                or not isinstance(epic, str) or not epic.strip()
                or not approver or not reason):
            raise ValueError("Invalid budget override")
        with sqlite3.connect(self.database, timeout=30, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute(
                    """SELECT 1 FROM budget_overrides
                       WHERE task_id=? AND used_at IS NULL""", (task_id,)).fetchone()
                if existing:
                    raise ValueError("Unused budget override already exists for this task")
                try:
                    db.execute("""INSERT INTO budget_overrides
                        (task_id,epic,month,amount,approver,reason,created_at)
                        VALUES (?,?,?,?,?,?,?)""",
                        (task_id, epic, month, str(amount), approver, reason,
                         datetime.now(timezone.utc).isoformat()))
                except sqlite3.IntegrityError as exc:
                    raise ValueError(
                        "Unused budget override already exists for this task") from exc
                db.execute("COMMIT")
            except Exception:
                self._rollback(db)
                raise

    def reserve(self, task_id: str, epic: str, amount: Decimal, *,
                month: str | None = None, charge_type: str = "additional") -> dict:
        """Reserve an estimate. Matching an unused override is local and does not call GitHub."""
        month = self._month(month)
        if not amount.is_finite() or amount < 0 or not task_id or not epic:
            raise ValueError("Invalid budget reservation")
        if charge_type not in ("additional", "subscription"):
            raise ValueError("Invalid charge type")
        with sqlite3.connect(self.database, timeout=30, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                rows = db.execute("""SELECT epic,estimated_aud,actual_aud FROM aud_reservations
                    WHERE month=? AND charge_type='additional'""", (month,)).fetchall()
                total = sum((Decimal(actual if actual is not None else estimated)
                             for _, estimated, actual in rows), Decimal("0"))
                epic_total = sum((Decimal(actual if actual is not None else estimated)
                                  for row_epic, estimated, actual in rows if row_epic == epic),
                                 Decimal("0"))
                override_id = None
                if charge_type == "additional" and (
                    total + amount > self.policy.cap_aud or
                    epic_total + amount > self.policy.epic_cap_aud):
                    override_id = _matching_override(db, task_id, epic, month, amount)
                    if override_id is None:
                        raise ValueError("Budget approval required; new task paused")
                created_at = datetime.now(timezone.utc).isoformat()
                db.execute("""INSERT INTO aud_reservations
                    (task_id,epic,month,estimated_aud,charge_type,created_at)
                    VALUES(?,?,?,?,?,?)""",
                    (task_id, epic, month, str(amount), charge_type, created_at))
                if override_id is not None:
                    updated = db.execute(
                        """UPDATE budget_overrides SET used_at=?
                           WHERE id=? AND used_at IS NULL""",
                        (created_at, override_id))
                    if updated.rowcount != 1:
                        raise ValueError("Budget approval required; new task paused")
                db.execute("COMMIT")
            except Exception:
                self._rollback(db)
                raise
        return self.status(month)

    def reconcile(self, task_id: str, actual_aud: Decimal) -> None:
        if not actual_aud.is_finite() or actual_aud < 0:
            raise ValueError("Invalid actual cost")
        with sqlite3.connect(self.database) as db:
            result = db.execute("""UPDATE aud_reservations SET actual_aud=?
                WHERE task_id=? AND actual_aud IS NULL""", (str(actual_aud),task_id))
            if result.rowcount != 1:
                raise ValueError("Unknown or already reconciled task")


def _matching_override(db: sqlite3.Connection, task_id: str, epic: str, month: str,
                       amount: Decimal) -> int | None:
    rows = db.execute(
        """SELECT id, amount FROM budget_overrides
           WHERE task_id=? AND epic=? AND month=? AND used_at IS NULL""",
        (task_id, epic, month)).fetchall()
    for row_id, stored in rows:
        if Decimal(stored) == amount:
            return row_id
    return None
