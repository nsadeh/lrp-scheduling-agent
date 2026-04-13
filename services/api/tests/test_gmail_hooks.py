"""Unit tests for email event classification logic."""

from datetime import UTC, datetime

from api.gmail.hooks import (
    EmailEvent,
    LoggingHook,
    MessageDirection,
    MessageType,
    classify_direction,
    classify_message_type,
)
from api.gmail.models import EmailAddress, Message


def _make_message(
    *,
    from_email: str = "sender@example.com",
    to: list[str] | None = None,
    cc: list[str] | None = None,
    message_id_header: str | None = "msg-1",
    msg_id: str = "m1",
    thread_id: str = "t1",
    date: datetime | None = None,
) -> Message:
    """Helper to build a Message for testing."""
    return Message(
        id=msg_id,
        thread_id=thread_id,
        subject="Test",
        **{"from": EmailAddress(email=from_email)},
        to=[EmailAddress(email=e) for e in (to or ["recipient@example.com"])],
        cc=[EmailAddress(email=e) for e in (cc or [])],
        date=date or datetime(2026, 1, 1, tzinfo=UTC),
        body_text="test body",
        message_id_header=message_id_header,
    )


class TestClassifyDirection:
    def test_incoming_message(self):
        msg = _make_message(from_email="external@example.com")
        assert classify_direction(msg, "coordinator@lrp.com") == MessageDirection.INCOMING

    def test_outgoing_message(self):
        msg = _make_message(from_email="coordinator@lrp.com")
        assert classify_direction(msg, "coordinator@lrp.com") == MessageDirection.OUTGOING

    def test_case_insensitive(self):
        msg = _make_message(from_email="Coordinator@LRP.com")
        assert classify_direction(msg, "coordinator@lrp.com") == MessageDirection.OUTGOING


class TestClassifyMessageType:
    def test_new_thread_no_prior_messages(self):
        msg = _make_message()
        msg_type, new_participants = classify_message_type(msg, [])
        assert msg_type == MessageType.NEW_THREAD
        assert new_participants == []

    def test_reply_same_participants(self):
        prior = _make_message(
            from_email="alice@example.com",
            to=["bob@example.com"],
            date=datetime(2026, 1, 1, tzinfo=UTC),
        )
        reply = _make_message(
            from_email="bob@example.com",
            to=["alice@example.com"],
            msg_id="m2",
            date=datetime(2026, 1, 2, tzinfo=UTC),
        )
        msg_type, new_participants = classify_message_type(reply, [prior])
        assert msg_type == MessageType.REPLY
        assert new_participants == []

    def test_forward_adds_new_participant(self):
        prior = _make_message(
            from_email="alice@example.com",
            to=["coordinator@lrp.com"],
            date=datetime(2026, 1, 1, tzinfo=UTC),
        )
        forward = _make_message(
            from_email="coordinator@lrp.com",
            to=["recruiter@lrp.com"],
            cc=["alice@example.com"],
            msg_id="m2",
            date=datetime(2026, 1, 2, tzinfo=UTC),
        )
        msg_type, new_participants = classify_message_type(forward, [prior])
        assert msg_type == MessageType.FORWARD
        assert len(new_participants) == 1
        assert new_participants[0].email == "recruiter@lrp.com"

    def test_reply_all_no_new_participants(self):
        prior = _make_message(
            from_email="alice@example.com",
            to=["bob@example.com", "carol@example.com"],
            date=datetime(2026, 1, 1, tzinfo=UTC),
        )
        reply = _make_message(
            from_email="bob@example.com",
            to=["alice@example.com", "carol@example.com"],
            msg_id="m2",
            date=datetime(2026, 1, 2, tzinfo=UTC),
        )
        msg_type, _new_participants = classify_message_type(reply, [prior])
        assert msg_type == MessageType.REPLY

    def test_case_insensitive_participant_matching(self):
        prior = _make_message(
            from_email="Alice@Example.com",
            to=["Bob@Example.com"],
            date=datetime(2026, 1, 1, tzinfo=UTC),
        )
        reply = _make_message(
            from_email="bob@example.com",
            to=["alice@example.com"],
            msg_id="m2",
            date=datetime(2026, 1, 2, tzinfo=UTC),
        )
        msg_type, _ = classify_message_type(reply, [prior])
        assert msg_type == MessageType.REPLY

    def test_forward_with_multiple_new_participants(self):
        prior = _make_message(
            from_email="alice@example.com",
            to=["coordinator@lrp.com"],
            date=datetime(2026, 1, 1, tzinfo=UTC),
        )
        forward = _make_message(
            from_email="coordinator@lrp.com",
            to=["new1@example.com", "new2@example.com"],
            msg_id="m2",
            date=datetime(2026, 1, 2, tzinfo=UTC),
        )
        msg_type, new_participants = classify_message_type(forward, [prior])
        assert msg_type == MessageType.FORWARD
        assert len(new_participants) == 2

    def test_cumulative_participants_across_multiple_messages(self):
        """Reply-all that includes someone from an earlier message is not a forward."""
        msg1 = _make_message(
            from_email="alice@example.com",
            to=["coordinator@lrp.com"],
            msg_id="m1",
            date=datetime(2026, 1, 1, tzinfo=UTC),
        )
        msg2 = _make_message(
            from_email="coordinator@lrp.com",
            to=["alice@example.com"],
            cc=["bob@example.com"],
            msg_id="m2",
            date=datetime(2026, 1, 2, tzinfo=UTC),
        )
        msg3 = _make_message(
            from_email="alice@example.com",
            to=["coordinator@lrp.com", "bob@example.com"],
            msg_id="m3",
            date=datetime(2026, 1, 3, tzinfo=UTC),
        )
        msg_type, _ = classify_message_type(msg3, [msg1, msg2])
        assert msg_type == MessageType.REPLY


class TestEmailEvent:
    def test_event_serialization(self):
        msg = _make_message()
        event = EmailEvent(
            message=msg,
            coordinator_email="coordinator@lrp.com",
            direction=MessageDirection.INCOMING,
            message_type=MessageType.NEW_THREAD,
            new_participants=[],
        )
        assert event.direction == MessageDirection.INCOMING
        assert event.message_type == MessageType.NEW_THREAD


class TestLoggingHook:
    async def test_logging_hook_does_not_raise(self):
        msg = _make_message()
        event = EmailEvent(
            message=msg,
            coordinator_email="coordinator@lrp.com",
            direction=MessageDirection.INCOMING,
            message_type=MessageType.NEW_THREAD,
            new_participants=[],
        )
        hook = LoggingHook()
        await hook.on_email(event)  # Should not raise
