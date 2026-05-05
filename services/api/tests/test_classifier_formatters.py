"""Tests for classifier prompt context formatters."""

from datetime import UTC, datetime

from api.classifier.formatters import (
    format_active_loops,
    format_email,
    format_events,
    format_loop_state,
    format_stage_states,
    format_thread_history,
)
from api.gmail.models import EmailAddress, Message
from api.scheduling.models import (
    Candidate,
    ClientContact,
    Contact,
    EventType,
    Loop,
    LoopEvent,
    StageState,
)


def _msg(
    msg_id: str = "msg1",
    from_email: str = "alice@example.com",
    from_name: str | None = "Alice",
    subject: str = "Interview",
    body: str = "Hello world",
    date: datetime | None = None,
) -> Message:
    return Message(
        id=msg_id,
        thread_id="thread1",
        subject=subject,
        **{"from": EmailAddress(name=from_name, email=from_email)},
        to=[EmailAddress(name="Bob", email="bob@example.com")],
        cc=[EmailAddress(email="cc@example.com")],
        date=date or datetime(2026, 4, 15, 10, 0, tzinfo=UTC),
        body_text=body,
    )


def _loop(loop_id: str = "lop_abc", title: str = "Round 1 - John Smith") -> Loop:
    return Loop(
        id=loop_id,
        coordinator_id="crd_1",
        client_contact_id="cli_1",
        recruiter_id="con_1",
        candidate_id="can_1",
        title=title,
        state=StageState.AWAITING_CANDIDATE,
        created_at=datetime(2026, 4, 10, tzinfo=UTC),
        updated_at=datetime(2026, 4, 14, tzinfo=UTC),
        candidate=Candidate(
            id="can_1", name="John Smith", created_at=datetime(2026, 4, 10, tzinfo=UTC)
        ),
        client_contact=ClientContact(
            id="cli_1",
            name="Jane Doe",
            email="jane@hedgefund.com",
            company="Hedge Fund Co",
            created_at=datetime(2026, 4, 10, tzinfo=UTC),
        ),
        recruiter=Contact(
            id="con_1",
            name="Bob Recruiter",
            email="bob@lrp.com",
            role="recruiter",
            created_at=datetime(2026, 4, 10, tzinfo=UTC),
        ),
    )


class TestFormatEmail:
    def test_includes_all_headers(self):
        result = format_email(_msg(), "incoming")
        assert "From: Alice <alice@example.com>" in result
        assert "To: Bob <bob@example.com>" in result
        assert "CC: cc@example.com" in result
        assert "Subject: Interview" in result
        assert "Direction: incoming" in result
        assert "Hello world" in result

    def test_no_cc_omits_line(self):
        msg = _msg()
        msg.cc = []
        result = format_email(msg, "outgoing")
        assert "CC:" not in result
        assert "Direction: outgoing" in result

    def test_includes_message_type(self):
        result = format_email(_msg(), "incoming", "forward")
        assert "Message-Type: forward" in result

    def test_omits_message_type_when_empty(self):
        result = format_email(_msg(), "incoming")
        assert "Message-Type" not in result


class TestFormatThreadHistory:
    def test_empty_thread(self):
        result = format_thread_history([], "msg1")
        assert "No prior" in result

    def test_excludes_current_message(self):
        msgs = [_msg("msg1"), _msg("msg2", body="Prior message")]
        result = format_thread_history(msgs, "msg1")
        assert "Prior message" in result
        # msg1 should not appear as a separate block
        assert result.count("---") == 2  # one block header

    def test_truncation(self):
        msgs = [
            _msg("msg1"),
            _msg("msg2", body="A" * 5000, date=datetime(2026, 4, 15, 9, 0, tzinfo=UTC)),
            _msg("msg3", body="B" * 5000, date=datetime(2026, 4, 15, 8, 0, tzinfo=UTC)),
            _msg("msg4", body="C" * 5000, date=datetime(2026, 4, 15, 7, 0, tzinfo=UTC)),
        ]
        result = format_thread_history(msgs, "msg1", char_budget=11_000)
        assert "truncated" in result


class TestFormatLoopState:
    def test_no_loop(self):
        result = format_loop_state(None)
        assert "No matching loop" in result

    def test_full_loop(self):
        result = format_loop_state(_loop())
        assert "John Smith" in result
        assert "Hedge Fund Co" in result
        assert "Bob Recruiter" in result
        assert "awaiting_candidate" in result


class TestFormatActiveLoops:
    def test_no_loops(self):
        result = format_active_loops([])
        assert "No active loops" in result

    def test_with_loops(self):
        result = format_active_loops([_loop()])
        assert "Round 1 - John Smith" in result
        assert "John Smith" in result
        assert "Hedge Fund Co" in result


class TestFormatEvents:
    def test_no_events(self):
        result = format_events([])
        assert "No events" in result

    def test_recent_events(self):
        events = [
            LoopEvent(
                id="evt_1",
                loop_id="lop_1",
                event_type=EventType.STATE_ADVANCED,
                data={},
                actor_email="alice@lrp.com",
                occurred_at=datetime(2026, 4, 14, 10, 0, tzinfo=UTC),
            )
        ]
        result = format_events(events)
        assert "state_advanced" in result
        assert "alice@lrp.com" in result


class TestFormatStaticContent:
    def test_stage_states_includes_all(self):
        result = format_stage_states()
        for state in StageState:
            assert state.value in result
