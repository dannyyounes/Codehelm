"""Conservative token/USD reservation ledger.

CLI providers may not expose reliable usage. Reservations are admission controls,
not provider-side spending limits. Unknown usage is never recorded as zero.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3
from datetime import datetime, timezone


@dataclass(frozen=True)
class Limits:
    tokens: int
    usd: Decimal

    def __post_init__(self):
        if self.tokens < 1 or not self.usd.is_finite() or self.usd <= 0:
            raise ValueError("Limits must be positive and finite")


class BudgetLedger:
    def __init__(self, database: Path):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS budget_reservations(
                prefix TEXT NOT NULL, task_id TEXT NOT NULL,
                provider TEXT NOT NULL, reserved_tokens INTEGER NOT NULL,
                reserved_usd TEXT NOT NULL, actual_tokens INTEGER,
                actual_usd TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(prefix,task_id))""")

    def report(self, prefix: str) -> dict:
        with sqlite3.connect(self.database) as db:
            rows = db.execute("""SELECT reserved_tokens,reserved_usd,actual_tokens,
                                actual_usd,status FROM budget_reservations WHERE prefix=?""",
                              (prefix,)).fetchall()
        tokens = sum(actual if actual is not None else reserved
                     for reserved, _, actual, _, _ in rows)
        dollars = sum((Decimal(actual) if actual is not None else Decimal(reserved))
                      for _, reserved, _, actual, _ in rows)
        return {"accounted_tokens": tokens, "accounted_usd": str(dollars),
                "unreconciled": sum(status != "reconciled" for *_, status in rows),
                "entries": len(rows)}

    def reserve(self, prefix: str, task_id: str, provider: str,
                tokens: int, usd: Decimal, limits: Limits) -> None:
        if tokens <= 0 or not usd.is_finite() or usd <= 0:
            raise ValueError("Reservation must be positive")
        with sqlite3.connect(self.database, timeout=30, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT reserved_tokens,reserved_usd,actual_tokens,actual_usd
                                 FROM budget_reservations WHERE prefix=?""", (prefix,)).fetchall()
            total_tokens = sum(a if a is not None else t for t, _, a, _ in rows)
            total_usd = sum(Decimal(a) if a is not None else Decimal(d)
                            for _, d, _, a in rows)
            if total_tokens + tokens > limits.tokens or total_usd + usd > limits.usd:
                db.execute("ROLLBACK")
                raise ValueError("Budget exhausted: reservation denied")
            try:
                db.execute("""INSERT INTO budget_reservations
                    (prefix,task_id,provider,reserved_tokens,reserved_usd,status,created_at)
                    VALUES (?,?,?,?,?,'reserved',?)""",
                    (prefix,task_id,provider,tokens,str(usd),
                     datetime.now(timezone.utc).isoformat()))
            except sqlite3.IntegrityError as exc:
                db.execute("ROLLBACK")
                raise ValueError("Task already has a budget reservation") from exc
            db.execute("COMMIT")

    def reconcile(self, prefix: str, task_id: str, tokens: int,
                  usd: Decimal) -> None:
        """Supply verified provider usage; no inference from CLI exit status."""
        if tokens < 0 or not usd.is_finite() or usd < 0:
            raise ValueError("Actual usage must be nonnegative and finite")
        with sqlite3.connect(self.database) as db:
            changed = db.execute("""UPDATE budget_reservations SET actual_tokens=?,
                actual_usd=?,status='reconciled' WHERE prefix=? AND task_id=?
                AND status='reserved'""", (tokens,str(usd),prefix,task_id))
            if changed.rowcount != 1:
                raise ValueError("No outstanding reservation for this task")
