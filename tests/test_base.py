"""Tests for clawless.channels.base."""

from clawless.channels.base import InboundMessage


def test_inbound_message_minimal():
    """InboundMessage can be created with just sender."""
    msg = InboundMessage(sender="whatsapp:+1234567890")
    assert msg.sender == "whatsapp:+1234567890"
    assert msg.sender_name == ""
    assert msg.content == ""
    assert msg.media_files == []
    assert msg.metadata == {}


def test_inbound_message_full():
    """InboundMessage with all fields populated."""
    msg = InboundMessage(
        sender="telegram:123456",
        sender_name="John",
        content="hello",
        media_files=["/tmp/photo.jpg"],
        metadata={"message_id": "abc"},
    )
    assert msg.sender == "telegram:123456"
    assert msg.sender_name == "John"
    assert msg.content == "hello"
    assert msg.media_files == ["/tmp/photo.jpg"]
    assert msg.metadata == {"message_id": "abc"}


def test_sender_is_channel_namespaced():
    """Different channels produce distinct sender identities."""
    wa = InboundMessage(sender="whatsapp:+1234567890")
    tg = InboundMessage(sender="telegram:1234567890")
    assert wa.sender != tg.sender
