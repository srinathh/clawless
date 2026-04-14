"""Unit tests for /reset_agent and /reset_queue commands.

Commands are intercepted before the SDK is called, so no ANTHROPIC_API_KEY
is required and no network calls are made.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from clawless.agent import AgentManager, RESET_AGENT_CMD, RESET_QUEUE_CMD
from clawless.channels.base import InboundMessage
from clawless.config import ClaudeConfig
from clawless.store import MessageStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeChannel:
    name = "test"
    formatting_instructions = "plain text"

    def __init__(self):
        self.sent: list[str] = []

    async def send(self, to: str, text: str = "", media: list[str] | None = None) -> None:
        self.sent.append(text)


def _make_agent(tmp_path: Path) -> tuple[AgentManager, MessageStore]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    store = MessageStore(data_dir / "test.db")
    agent = AgentManager(
        config=ClaudeConfig(),
        plugins=[],
        workspace=workspace,
        data_dir=data_dir,
        store=store,
    )
    return agent, store


def _msg(sender: str, content: str, msg_id: str) -> InboundMessage:
    return InboundMessage(sender=sender, content=content, message_id=msg_id)


SENDER = "test:user1"


# ---------------------------------------------------------------------------
# /reset_agent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reset_agent_clears_session(tmp_path):
    agent, store = _make_agent(tmp_path)
    channel = _FakeChannel()

    store.set_session(SENDER, "session-abc")
    store.store_message("msg-1", SENDER, RESET_AGENT_CMD, inbound=True)

    await agent.process_message(_msg(SENDER, RESET_AGENT_CMD, "msg-1"), channel)

    assert store.get_session(SENDER) is None, "session should be deleted"
    assert store.get_cursor(SENDER) == "msg-1", "cursor should be advanced past the command"
    assert len(channel.sent) == 1
    assert "reset" in channel.sent[0].lower()


@pytest.mark.asyncio
async def test_reset_agent_with_no_existing_session(tmp_path):
    agent, store = _make_agent(tmp_path)
    channel = _FakeChannel()

    # No session stored — should still succeed without error
    store.store_message("msg-1", SENDER, RESET_AGENT_CMD, inbound=True)
    await agent.process_message(_msg(SENDER, RESET_AGENT_CMD, "msg-1"), channel)

    assert store.get_session(SENDER) is None
    assert len(channel.sent) == 1


@pytest.mark.asyncio
async def test_reset_agent_does_not_call_sdk(tmp_path):
    """Completes without ANTHROPIC_API_KEY — SDK is never called."""
    agent, store = _make_agent(tmp_path)
    channel = _FakeChannel()

    store.store_message("msg-1", SENDER, RESET_AGENT_CMD, inbound=True)
    # If this completes without raising (e.g. AuthenticationError), SDK was not called.
    await agent.process_message(_msg(SENDER, RESET_AGENT_CMD, "msg-1"), channel)
    assert len(channel.sent) == 1


# ---------------------------------------------------------------------------
# /reset_queue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reset_queue_advances_cursor_to_latest(tmp_path):
    agent, store = _make_agent(tmp_path)
    channel = _FakeChannel()

    # Queue: msg-1, msg-2 (unprocessed), then reset, then msg-4 (arrived after reset)
    for i, cid in enumerate(["msg-1", "msg-2", "msg-reset", "msg-4"], 1):
        content = f"message {i}" if cid != "msg-reset" else RESET_QUEUE_CMD
        store.store_message(cid, SENDER, content, inbound=True)

    await agent.process_message(_msg(SENDER, RESET_QUEUE_CMD, "msg-reset"), channel)

    # Cursor should land on the latest message (msg-4), skipping it too
    assert store.get_cursor(SENDER) == "msg-4"
    assert len(channel.sent) == 1
    assert "cleared" in channel.sent[0].lower()


@pytest.mark.asyncio
async def test_reset_queue_nothing_pending_after_cmd(tmp_path):
    """When reset_queue is the latest message, cursor stops there."""
    agent, store = _make_agent(tmp_path)
    channel = _FakeChannel()

    store.store_message("msg-1", SENDER, "hello", inbound=True)
    store.store_message("msg-reset", SENDER, RESET_QUEUE_CMD, inbound=True)

    await agent.process_message(_msg(SENDER, RESET_QUEUE_CMD, "msg-reset"), channel)

    assert store.get_cursor(SENDER) == "msg-reset"
    assert len(channel.sent) == 1
    assert "cleared" in channel.sent[0].lower()


@pytest.mark.asyncio
async def test_reset_queue_only_message(tmp_path):
    """reset_queue as the very first message — no pending queue at all."""
    agent, store = _make_agent(tmp_path)
    channel = _FakeChannel()

    store.store_message("msg-reset", SENDER, RESET_QUEUE_CMD, inbound=True)

    await agent.process_message(_msg(SENDER, RESET_QUEUE_CMD, "msg-reset"), channel)

    assert store.get_cursor(SENDER) == "msg-reset"
    assert len(channel.sent) == 1


@pytest.mark.asyncio
async def test_reset_queue_does_not_call_sdk(tmp_path):
    """Completes without ANTHROPIC_API_KEY — SDK is never called."""
    agent, store = _make_agent(tmp_path)
    channel = _FakeChannel()

    store.store_message("msg-reset", SENDER, RESET_QUEUE_CMD, inbound=True)
    await agent.process_message(_msg(SENDER, RESET_QUEUE_CMD, "msg-reset"), channel)
    assert len(channel.sent) == 1


@pytest.mark.asyncio
async def test_reset_queue_skipped_count_in_reply(tmp_path):
    """When there are messages after the command, the count appears in the reply."""
    agent, store = _make_agent(tmp_path)
    channel = _FakeChannel()

    store.store_message("msg-reset", SENDER, RESET_QUEUE_CMD, inbound=True)
    # Two messages arrived after the command (e.g. rapid-fire from user)
    store.store_message("msg-after-1", SENDER, "oops", inbound=True)
    store.store_message("msg-after-2", SENDER, "ignore that", inbound=True)

    await agent.process_message(_msg(SENDER, RESET_QUEUE_CMD, "msg-reset"), channel)

    assert store.get_cursor(SENDER) == "msg-after-2"
    # Should mention the 1 message between cursor and latest (msg-after-1, exclusive)
    # msg-after-2 is the new cursor position itself, so skipped count = 1
    assert "1" in channel.sent[0]


# ---------------------------------------------------------------------------
# skip_to_latest store method
# ---------------------------------------------------------------------------

def test_skip_to_latest_advances_to_latest(tmp_path):
    store = MessageStore(tmp_path / "test.db")
    sender = "test:u"

    for i, mid in enumerate(["a", "b", "c", "d"]):
        store.store_message(mid, sender, f"msg {i}", inbound=True)

    # Simulate cursor already at "b" (command message)
    store.set_cursor(sender, "b")

    skipped = store.skip_to_latest(sender)

    assert store.get_cursor(sender) == "d"
    assert skipped == 1  # only "c" is strictly between "b" and "d"


def test_skip_to_latest_already_at_latest(tmp_path):
    store = MessageStore(tmp_path / "test.db")
    sender = "test:u"

    store.store_message("only", sender, "msg", inbound=True)
    store.set_cursor(sender, "only")

    skipped = store.skip_to_latest(sender)

    assert store.get_cursor(sender) == "only"
    assert skipped == 0


def test_skip_to_latest_no_messages(tmp_path):
    store = MessageStore(tmp_path / "test.db")
    skipped = store.skip_to_latest("test:nobody")
    assert skipped == 0
