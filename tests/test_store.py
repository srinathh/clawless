"""Unit tests for MessageStore — no API key needed."""

import sqlite3
import uuid
from pathlib import Path

import pytest

from clawless.store import MessageStore


@pytest.fixture
def store(tmp_path: Path) -> MessageStore:
    s = MessageStore(tmp_path / "test.db")
    yield s
    s.close()


class TestWalMode:
    def test_wal_enabled(self, store: MessageStore):
        mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"


class TestSessions:
    def test_get_missing_returns_none(self, store: MessageStore):
        assert store.get_session("nobody") is None

    def test_set_and_get(self, store: MessageStore):
        store.set_session("user1", "session-abc")
        assert store.get_session("user1") == "session-abc"

    def test_overwrite(self, store: MessageStore):
        store.set_session("user1", "old")
        store.set_session("user1", "new")
        assert store.get_session("user1") == "new"


class TestMessages:
    def test_store_returns_true_on_insert(self, store: MessageStore):
        assert store.store_message(
            id="msg1", sender="test:user1", content="hello", inbound=True
        )

    def test_store_returns_false_on_duplicate(self, store: MessageStore):
        store.store_message(id="msg1", sender="test:user1", content="hello", inbound=True)
        assert not store.store_message(
            id="msg1", sender="test:user1", content="hello again", inbound=True
        )

    def test_store_with_media(self, store: MessageStore):
        store.store_message(
            id="msg-media",
            sender="test:user1",
            content="pic",
            inbound=True,
            media_files=["/path/to/img.jpg"],
        )
        rows = store.get_unprocessed("test:user1")
        assert len(rows) == 1
        assert rows[0]["media_files"] == '["/path/to/img.jpg"]'

    def test_store_with_sender_name(self, store: MessageStore):
        store.store_message(
            id="msg-name",
            sender="test:user1",
            content="hi",
            inbound=True,
            sender_name="Alice",
        )
        rows = store.get_unprocessed("test:user1")
        assert rows[0]["sender_name"] == "Alice"

    def test_multiple_messages_different_ids(self, store: MessageStore):
        for i in range(5):
            assert store.store_message(
                id=f"msg-{i}", sender="test:user1", content=f"msg {i}", inbound=True
            )
        rows = store.get_unprocessed("test:user1")
        assert len(rows) == 5


class TestCursors:
    def test_get_missing_returns_none(self, store: MessageStore):
        assert store.get_cursor("nobody") is None

    def test_set_and_get(self, store: MessageStore):
        store.set_cursor("user1", "msg-3")
        assert store.get_cursor("user1") == "msg-3"

    def test_overwrite(self, store: MessageStore):
        store.set_cursor("user1", "msg-1")
        store.set_cursor("user1", "msg-5")
        assert store.get_cursor("user1") == "msg-5"

    def test_rollback(self, store: MessageStore):
        """Simulate cursor rollback on error (nanoclaw pattern)."""
        store.set_cursor("user1", "msg-1")
        # Advance optimistically
        store.set_cursor("user1", "msg-3")
        assert store.get_cursor("user1") == "msg-3"
        # Rollback
        store.set_cursor("user1", "msg-1")
        assert store.get_cursor("user1") == "msg-1"


class TestUnprocessed:
    def _seed(self, store: MessageStore, count: int = 3):
        for i in range(count):
            store.store_message(
                id=f"msg-{i}", sender="test:user1", content=f"msg {i}", inbound=True
            )

    def test_all_unprocessed_when_no_cursor(self, store: MessageStore):
        self._seed(store)
        rows = store.get_unprocessed("test:user1")
        assert len(rows) == 3

    def test_unprocessed_after_cursor(self, store: MessageStore):
        self._seed(store, 5)
        # Set cursor to msg-2 — should get msg-3 and msg-4
        store.set_cursor("test:user1", "msg-2")
        rows = store.get_unprocessed("test:user1")
        assert len(rows) == 2
        assert rows[0]["id"] == "msg-3"
        assert rows[1]["id"] == "msg-4"

    def test_no_unprocessed_when_cursor_at_last(self, store: MessageStore):
        self._seed(store)
        store.set_cursor("test:user1", "msg-2")
        rows = store.get_unprocessed("test:user1")
        assert len(rows) == 0

    def test_outbound_excluded(self, store: MessageStore):
        store.store_message(id="in-1", sender="test:user1", content="hi", inbound=True)
        store.store_message(id="out-1", sender="test:user1", content="hello", inbound=False)
        rows = store.get_unprocessed("test:user1")
        assert len(rows) == 1
        assert rows[0]["id"] == "in-1"


class TestSendersWithUnprocessed:
    def test_empty_store(self, store: MessageStore):
        assert store.get_all_senders_with_unprocessed() == []

    def test_sender_with_no_cursor(self, store: MessageStore):
        store.store_message(id="m1", sender="test:user1", content="hi", inbound=True)
        senders = store.get_all_senders_with_unprocessed()
        assert "test:user1" in senders

    def test_sender_fully_processed(self, store: MessageStore):
        store.store_message(id="m1", sender="test:user1", content="hi", inbound=True)
        store.set_cursor("test:user1", "m1")
        senders = store.get_all_senders_with_unprocessed()
        assert "test:user1" not in senders

    def test_sender_partially_processed(self, store: MessageStore):
        store.store_message(id="m1", sender="test:user1", content="hi", inbound=True)
        store.store_message(id="m2", sender="test:user1", content="hey", inbound=True)
        store.set_cursor("test:user1", "m1")
        senders = store.get_all_senders_with_unprocessed()
        assert "test:user1" in senders

    def test_multiple_senders(self, store: MessageStore):
        store.store_message(id="a1", sender="test:alice", content="hi", inbound=True)
        store.store_message(id="b1", sender="whatsapp:bob", content="yo", inbound=True)
        store.set_cursor("test:alice", "a1")  # alice fully processed
        senders = store.get_all_senders_with_unprocessed()
        assert "test:alice" not in senders
        assert "whatsapp:bob" in senders
