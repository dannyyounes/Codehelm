"""SQLite storage for DesignFlow screens and comments.

Tables live in `.codehelm/designflow.db`, separate from the task database.
"""

from pathlib import Path
import json
import sqlite3
from datetime import datetime, timezone

from codehelm.designflow.render import render_screen
from codehelm.designflow.spec import (
    DesignFlowError, MockupComment, Screen, ScreenSpec, check_screen_id, clean_comment_text,
    parse_spec,
)


def default_paths(state_dir: Path = Path(".codehelm")) -> tuple[Path, Path]:
    root = Path(state_dir)
    return root / "designflow.db", root / "designflow"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DesignFlowStore:
    def __init__(self, database: Path, root: Path):
        self.database = Path(database)
        self.root = Path(root)

    def initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database, timeout=5) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS designflow_screens (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                brief TEXT NOT NULL,
                revision INTEGER NOT NULL,
                spec_json TEXT NOT NULL,
                html_path TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS designflow_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                screen_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                element_uid TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                queued_at TEXT,
                consumed_revision INTEGER
            )""")

    def get(self, screen_id: str) -> Screen | None:
        check_screen_id(screen_id)
        self.initialize()
        with sqlite3.connect(self.database, timeout=5) as db:
            row = db.execute(
                "SELECT id, revision, spec_json, html_path, updated_at FROM designflow_screens WHERE id=?",
                (screen_id,),
            ).fetchone()
        if row is None:
            return None
        return Screen(row[0], row[1], parse_spec(json.loads(row[2])), row[3], row[4])

    def require(self, screen_id: str) -> Screen:
        screen = self.get(screen_id)
        if screen is None:
            raise DesignFlowError(f"Unknown screen: {screen_id}")
        return screen

    def create(self, screen_id: str, spec: ScreenSpec) -> Screen:
        screen_id = check_screen_id(screen_id)
        self.initialize()
        if self.get(screen_id) is not None:
            raise DesignFlowError(f"Screen already exists: {screen_id}")
        path = self._html_path(screen_id, 1)
        stamp = _now()
        screen = Screen(screen_id, 1, spec, str(path), stamp)
        with sqlite3.connect(self.database, timeout=5) as db:
            try:
                db.execute(
                    """INSERT INTO designflow_screens
                       (id, title, brief, revision, spec_json, html_path, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (screen_id, spec.title, spec.brief, 1, json.dumps(spec.to_dict()), str(path), stamp),
                )
            except sqlite3.IntegrityError as exc:
                raise DesignFlowError(f"Screen already exists: {screen_id}") from exc
        try:
            self._publish(screen, ())
        except OSError:
            with sqlite3.connect(self.database, timeout=5) as db:
                db.execute("DELETE FROM designflow_screens WHERE id=?", (screen_id,))
            raise
        return self.require(screen_id)

    def add_comment(self, screen_id: str, element_uid: str, text: str) -> MockupComment:
        screen = self.require(screen_id)
        element_uid = _known_element(screen.spec, element_uid)
        cleaned = clean_comment_text(text)
        if len(self.open_comments(screen_id)) >= 100:
            raise DesignFlowError("This mockup already has 100 open comments")
        stamp = _now()
        with sqlite3.connect(self.database, timeout=5) as db:
            cursor = db.execute(
                """INSERT INTO designflow_comments
                   (screen_id, revision, element_uid, text, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (screen_id, screen.revision, element_uid, cleaned, stamp),
            )
            comment_id = int(cursor.lastrowid)
        self._republish(screen_id)
        return self._comment(comment_id)

    def queue_comments(self, screen_id: str) -> list[MockupComment]:
        """Mark every open comment as sent. Does not call a provider."""
        screen = self.require(screen_id)
        stamp = _now()
        with sqlite3.connect(self.database, timeout=5) as db:
            db.execute(
                """UPDATE designflow_comments
                   SET queued_at=?
                   WHERE screen_id=? AND queued_at IS NULL AND consumed_revision IS NULL""",
                (stamp, screen_id),
            )
        self._republish(screen_id)
        del screen
        return self.queued_comments(screen_id)

    def open_comments(self, screen_id: str) -> list[MockupComment]:
        self.require(screen_id)
        with sqlite3.connect(self.database, timeout=5) as db:
            rows = db.execute(
                """SELECT id, screen_id, revision, element_uid, text, created_at, queued_at, consumed_revision
                   FROM designflow_comments
                   WHERE screen_id=? AND consumed_revision IS NULL
                   ORDER BY id""",
                (screen_id,),
            ).fetchall()
        return [_row_comment(row) for row in rows]

    def queued_comments(self, screen_id: str) -> list[MockupComment]:
        return [comment for comment in self.open_comments(screen_id) if comment.queued]

    def replace(self, screen_id: str, spec: ScreenSpec) -> Screen:
        """Store a revised spec and consume the comments that were sent."""
        screen = self.require(screen_id)
        queued = self.queued_comments(screen_id)
        if not queued:
            raise DesignFlowError("No comments have been sent to the agent. Run designflow send first.")
        revision = screen.revision + 1
        path = self._html_path(screen_id, revision)
        stamp = _now()
        remaining = [comment for comment in self.open_comments(screen_id) if not comment.queued]
        updated = Screen(screen_id, revision, spec, str(path), stamp)
        temporary = path.with_suffix(".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(render_screen(updated.id, updated.revision, updated.spec, remaining), encoding="utf-8")
        try:
            with sqlite3.connect(self.database, timeout=5) as db:
                db.execute("BEGIN IMMEDIATE")
                cursor = db.execute(
                    """UPDATE designflow_screens
                       SET title=?, brief=?, revision=?, spec_json=?, html_path=?, updated_at=?
                       WHERE id=? AND revision=?""",
                    (spec.title, spec.brief, revision, json.dumps(spec.to_dict()), str(path), stamp,
                     screen_id, screen.revision),
                )
                if cursor.rowcount != 1:
                    raise DesignFlowError("Screen changed during revision")
                db.execute(
                    """UPDATE designflow_comments
                       SET consumed_revision=?
                       WHERE screen_id=? AND queued_at IS NOT NULL AND consumed_revision IS NULL""",
                    (revision, screen_id),
                )
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        temporary.replace(path)
        return self.require(screen_id)

    def _html_path(self, screen_id: str, revision: int) -> Path:
        return (self.root / screen_id / f"r{revision}.html").resolve()

    def _publish(self, screen: Screen, comments: list[MockupComment] | tuple[MockupComment, ...]) -> None:
        path = Path(screen.html_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        html = render_screen(screen.id, screen.revision, screen.spec, comments)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(html, encoding="utf-8")
        temporary.replace(path)

    def _republish(self, screen_id: str) -> None:
        screen = self.require(screen_id)
        self._publish(screen, self.open_comments(screen_id))

    def _comment(self, comment_id: int) -> MockupComment:
        with sqlite3.connect(self.database, timeout=5) as db:
            row = db.execute(
                """SELECT id, screen_id, revision, element_uid, text, created_at, queued_at, consumed_revision
                   FROM designflow_comments WHERE id=?""",
                (comment_id,),
            ).fetchone()
        if row is None:
            raise DesignFlowError("Comment was not stored")
        return _row_comment(row)


def _known_element(spec: ScreenSpec, element_uid: str) -> str:
    if element_uid == "screen":
        return element_uid
    if not isinstance(element_uid, str) or element_uid not in {field.uid for field in spec.fields}:
        raise DesignFlowError(f"Unknown element: {element_uid}")
    return element_uid


def _row_comment(row: tuple) -> MockupComment:
    return MockupComment(
        id=row[0], screen_id=row[1], revision=row[2], element_uid=row[3], text=row[4],
        created_at=row[5], queued_at=row[6], consumed_revision=row[7],
    )
