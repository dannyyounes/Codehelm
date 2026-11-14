"""Monthly AUD additional-charge budget, separate from subscription usage.

Admission reservations are estimates; not provider-side hard spending caps.
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
                created_at TEXT NOT NULL)""")

    def _month(self, month: str | None) -> str:
        return month or datetime.now(timezone.utc).strftime("%Y-%m")

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

    def reserve(self, task_id: str, epic: str, amount: Decimal, *,
                month: str | None = None, charge_type: str = "additional") -> dict:
        month = self._month(month)
        if not amount.is_finite() or amount < 0 or not task_id or not epic:
            raise ValueError("Invalid budget reservation")
        if charge_type not in ("additional", "subscription"):
            raise ValueError("Invalid charge type")
        with sqlite3.connect(self.database, timeout=30, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT epic,estimated_aud,actual_aud FROM aud_reservations
                WHERE month=? AND charge_type='additional'""", (month,)).fetchall()
            total = sum((Decimal(actual if actual is not None else estimated)
                         for _, estimated, actual in rows), Decimal("0"))
            epic_total = sum((Decimal(actual if actual is not None else estimated)
                              for row_epic, estimated, actual in rows if row_epic == epic),
                             Decimal("0"))
            if charge_type == "additional" and (
                total + amount > self.policy.cap_aud or
                epic_total + amount > self.policy.epic_cap_aud):
                db.execute("ROLLBACK")
                raise ValueError("Budget approval required; new task paused")
            db.execute("""INSERT INTO aud_reservations
                (task_id,epic,month,estimated_aud,charge_type,created_at)
                VALUES(?,?,?,?,?,?)""",
                (task_id,epic,month,str(amount),charge_type,
                 datetime.now(timezone.utc).isoformat()))
            db.execute("COMMIT")
        return self.status(month)

    def reconcile(self, task_id: str, actual_aud: Decimal) -> None:
        if not actual_aud.is_finite() or actual_aud < 0:
            raise ValueError("Invalid actual cost")
        with sqlite3.connect(self.database) as db:
            result = db.execute("""UPDATE aud_reservations SET actual_aud=?
                WHERE task_id=? AND actual_aud IS NULL""", (str(actual_aud),task_id))
            if result.rowcount != 1:
                raise ValueError("Unknown or already reconciled task")
