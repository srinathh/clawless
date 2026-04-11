"""SQLite message store for inbound messages, sessions, and cursors.

Provides a message bus (channels write, message loop reads), session
persistence (sender → agent session_id), and cursor-based crash recovery.
Uses WAL mode for concurrent read/write safety.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS sessions (
    sender      TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    sender      TEXT NOT NULL,
    inbound     INTEGER NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    media_files TEXT,
    sender_name TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_sender
    ON messages(sender, created_at);

CREATE TABLE IF NOT EXISTS cursors (
    sender      TEXT PRIMARY KEY,
    last_msg_id TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class MessageStore:
    """SQLite-backed store for messages, sessions, and cursors."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        logger.info("MessageStore opened at %s", db_path)

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def get_session(self, sender: str) -> str | None:
        row = self._conn.execute(
            "SELECT session_id FROM sessions WHERE sender = ?", (sender,)
        ).fetchone()
        return row["session_id"] if row else None

    def set_session(self, sender: str, session_id: str) -> None:
        self._conn.execute(
            "INSERT INTO sessions (sender, session_id, updated_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(sender) DO UPDATE SET session_id = excluded.session_id, "
            "updated_at = excluded.updated_at",
            (sender, session_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def store_message(
        self,
        id: str,
        sender: str,
        content: str,
        inbound: bool,
        sender_name: str = "",
        media_files: list[str] | None = None,
    ) -> bool:
        """Store a message. Returns True if inserted, False if duplicate (PK conflict)."""
        media_json = json.dumps(media_files) if media_files else None
        try:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO messages "
                "(id, sender, inbound, content, media_files, sender_name) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (id, sender, 1 if inbound else 0, content, media_json, sender_name),
            )
            self._conn.commit()
            return cur.rowcount > 0
        except sqlite3.Error:
            logger.exception("Error storing message %s for %s", id, sender)
            return False

    def get_unprocessed(self, sender: str) -> list[dict]:
        """Get inbound messages after the cursor for a given sender.

        Uses rowid ordering (monotonically increasing insertion order)
        rather than created_at timestamps to avoid same-second collisions.
        """
        cursor_id = self.get_cursor(sender)
        if cursor_id:
            # Get messages with rowid greater than the cursor message's rowid
            row = self._conn.execute(
                "SELECT rowid FROM messages WHERE id = ?", (cursor_id,)
            ).fetchone()
            if row:
                cursor_rowid = row["rowid"]
                rows = self._conn.execute(
                    "SELECT id, sender, content, sender_name, media_files, created_at "
                    "FROM messages "
                    "WHERE sender = ? AND inbound = 1 AND rowid > ? "
                    "ORDER BY rowid",
                    (sender, cursor_rowid),
                ).fetchall()
                return [dict(r) for r in rows]
        # No cursor — return all inbound messages for this sender
        rows = self._conn.execute(
            "SELECT id, sender, content, sender_name, media_files, created_at "
            "FROM messages WHERE sender = ? AND inbound = 1 ORDER BY rowid",
            (sender,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_senders_with_unprocessed(self) -> list[str]:
        """Return senders that have inbound messages past their cursor.

        Uses rowid ordering to avoid same-second timestamp collisions.
        """
        # Senders with messages but no cursor (never processed)
        rows_no_cursor = self._conn.execute(
            "SELECT DISTINCT m.sender FROM messages m "
            "LEFT JOIN cursors c ON m.sender = c.sender "
            "WHERE m.inbound = 1 AND c.sender IS NULL"
        ).fetchall()

        # Senders with messages after their cursor (by rowid)
        rows_with_cursor = self._conn.execute(
            "SELECT DISTINCT m.sender FROM messages m "
            "JOIN cursors c ON m.sender = c.sender "
            "JOIN messages cm ON cm.id = c.last_msg_id "
            "WHERE m.inbound = 1 AND m.rowid > cm.rowid"
        ).fetchall()

        senders = {row["sender"] for row in rows_no_cursor}
        senders.update(row["sender"] for row in rows_with_cursor)
        return list(senders)

    # ------------------------------------------------------------------
    # Cursors
    # ------------------------------------------------------------------

    def get_cursor(self, sender: str) -> str | None:
        row = self._conn.execute(
            "SELECT last_msg_id FROM cursors WHERE sender = ?", (sender,)
        ).fetchone()
        return row["last_msg_id"] if row else None

    def set_cursor(self, sender: str, msg_id: str) -> None:
        self._conn.execute(
            "INSERT INTO cursors (sender, last_msg_id, updated_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(sender) DO UPDATE SET last_msg_id = excluded.last_msg_id, "
            "updated_at = excluded.updated_at",
            (sender, msg_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()
        logger.info("MessageStore closed")
