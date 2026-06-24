"""Tests for classifier prompt context formatters."""

from datetime import UTC, datetime

from api.classifier.formatters import (
    format_active_loops,
    format_active_loops_xml,
    format_email,
    format_events,
    format_llm_datetime,
    format_loop_state,
    format_pending_suggestions,
    format_stage_states,
    format_thread_history,
)
from api.classifier.models import (
    EmailClassification,
    SuggestedAction,
    Suggestion,
    SuggestionStatus,
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

    def test_includes_to_and_cc_in_prior_messages(self):
        msgs = [
            _msg("msg1"),
            _msg("msg2", body="Prior message", date=datetime(2026, 4, 15, 9, 0, tzinfo=UTC)),
        ]
        result = format_thread_history(msgs, "msg1")
        assert "To: Bob <bob@example.com>" in result
        assert "CC: cc@example.com" in result

    def test_omits_empty_to_and_cc(self):
        msg = _msg("msg2", body="Prior message", date=datetime(2026, 4, 15, 9, 0, tzinfo=UTC))
        msg.to = []
        msg.cc = []
        result = format_thread_history([_msg("msg1"), msg], "msg1")
        assert "To:" not in result
        assert "CC:" not in result


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


def _loop_no_client_contact(
    loop_id: str = "lop_ats",
    title: str = "John Doe, Big Bank",
) -> Loop:
    """ATS-created loop: company is known and lives in the title, but there's
    no named client contact (common for Greenhouse-style stage updates)."""
    return Loop(
        id=loop_id,
        coordinator_id="crd_1",
        client_contact_id=None,
        recruiter_id=None,
        candidate_id="can_2",
        title=title,
        state=StageState.NEW,
        created_at=datetime(2026, 4, 10, tzinfo=UTC),
        updated_at=datetime(2026, 4, 10, tzinfo=UTC),
        candidate=Candidate(
            id="can_2", name="John Doe", created_at=datetime(2026, 4, 10, tzinfo=UTC)
        ),
        client_contact=None,
    )


class TestFormatActiveLoops:
    def test_no_loops(self):
        result = format_active_loops([])
        assert "No active loops" in result

    def test_with_loops(self):
        result = format_active_loops([_loop()])
        assert "Round 1 - John Smith" in result
        assert "John Smith" in result
        assert "Hedge Fund Co" in result

    def test_falls_back_to_title_company_when_no_client_contact(self):
        result = format_active_loops([_loop_no_client_contact()])
        assert "Client=Big Bank" in result
        assert "Unknown" not in result


class TestFormatActiveLoopsXml:
    def test_no_loops(self):
        assert "No active loops" in format_active_loops_xml([])

    def test_renders_client_company_from_contact(self):
        result = format_active_loops_xml([_loop()])
        assert "<client-company>Hedge Fund Co</client-company>" in result
        assert "<candidate>John Smith</candidate>" in result

    def test_falls_back_to_title_company_when_no_client_contact(self):
        """The bug: ATS-created loops have client_contact=None but the company
        IS in the title. Without the fallback, the classifier sees 'Unknown'
        and over-creates loops instead of LINK_THREAD-ing to this one.
        """
        result = format_active_loops_xml([_loop_no_client_contact()])
        assert "<client-company>Big Bank</client-company>" in result
        assert "Unknown" not in result


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

    def test_state_advanced_shows_transition(self):
        events = [
            LoopEvent(
                id="evt_1",
                loop_id="lop_1",
                event_type=EventType.STATE_ADVANCED,
                data={"from_state": "new", "to_state": "awaiting_candidate"},
                actor_email="alice@lrp.com",
                occurred_at=datetime(2026, 4, 14, 10, 0, tzinfo=UTC),
            )
        ]
        result = format_events(events)
        assert "new → awaiting_candidate" in result

    def test_cold_event_shows_reason(self):
        events = [
            LoopEvent(
                id="evt_2",
                loop_id="lop_1",
                event_type=EventType.LOOP_MARKED_COLD,
                data={"reason": "candidate withdrew"},
                actor_email="alice@lrp.com",
                occurred_at=datetime(2026, 4, 14, 10, 0, tzinfo=UTC),
            )
        ]
        result = format_events(events)
        assert "(candidate withdrew)" in result


class TestFormatStaticContent:
    def test_stage_states_includes_all(self):
        result = format_stage_states()
        for state in StageState:
            assert state.value in result


def _suggestion(
    loop_id="lop_1",
    action=SuggestedAction.ADVANCE_STAGE,
    action_data=None,
    summary="Move to next stage",
) -> Suggestion:
    if action_data is None:
        action_data = {"target_stage": "awaiting_client"}
    return Suggestion(
        id="sug_1",
        coordinator_email="coord@lrp.com",
        gmail_message_id="msg1",
        gmail_thread_id="thread1",
        loop_id=loop_id,
        classification=EmailClassification.AVAILABILITY_RESPONSE,
        action=action,
        confidence=0.9,
        summary=summary,
        action_data=action_data,
        status=SuggestionStatus.PENDING,
    )


class TestFormatPendingSuggestions:
    def test_empty_returns_default(self):
        result = format_pending_suggestions([])
        assert result == "No current pending suggestions."

    def test_multiple_suggestions_formatted(self):
        suggestions = [
            _suggestion(
                loop_id="lop_1",
                action=SuggestedAction.ADVANCE_STAGE,
                action_data={"target_stage": "awaiting_client"},
                summary="Move to awaiting client",
            ),
            _suggestion(
                loop_id="lop_2",
                action=SuggestedAction.DRAFT_EMAIL,
                action_data={"body": "Hi there", "recipient_type": "recruiter"},
                summary="Send availability request",
            ),
            _suggestion(
                loop_id="lop_1",
                action=SuggestedAction.ASK_COORDINATOR,
                action_data={"question": "Which time works?"},
                summary="Need clarification",
            ),
        ]
        result = format_pending_suggestions(suggestions)
        assert "Pending suggestions" in result
        assert "[lop_1] advance_stage: Move to awaiting client" in result
        assert "target_stage=awaiting_client" in result
        assert "[lop_2] draft_email: Send availability request" in result
        assert "to=recruiter" in result
        assert 'body="Hi there"' in result
        assert "[lop_1] ask_coordinator: Need clarification" in result
        assert 'question="Which time works?"' in result


class TestFormatLLMDatetime:
    """Renders the 'current date' context string passed to LLMs. Day-of-week
    must be explicit — bare ISO dates caused the model to hallucinate
    weekdays in scheduling drafts."""

    def test_renders_weekday_month_day_year_time_in_eastern(self):
        # 2026-05-14 00:02 UTC is 2026-05-13 20:02 ET (Wednesday).
        dt = datetime(2026, 5, 14, 0, 2, tzinfo=UTC)
        assert format_llm_datetime(dt) == "Wednesday, May 13 2026, 8:02 PM ET"

    def test_naive_datetime_treated_as_utc(self):
        dt = datetime(2026, 5, 14, 0, 2)
        assert format_llm_datetime(dt) == "Wednesday, May 13 2026, 8:02 PM ET"

    def test_morning_hour_strips_leading_zero(self):
        # 13:30 UTC → 09:30 ET (Wednesday, EDT).
        dt = datetime(2026, 5, 13, 13, 30, tzinfo=UTC)
        assert format_llm_datetime(dt) == "Wednesday, May 13 2026, 9:30 AM ET"

    def test_noon_renders_as_12_pm(self):
        # 16:00 UTC → 12:00 ET (Wednesday, EDT).
        dt = datetime(2026, 5, 13, 16, 0, tzinfo=UTC)
        assert format_llm_datetime(dt) == "Wednesday, May 13 2026, 12:00 PM ET"

    def test_midnight_renders_as_12_am(self):
        # 04:00 UTC → 00:00 ET (Wednesday, EDT).
        dt = datetime(2026, 5, 13, 4, 0, tzinfo=UTC)
        assert format_llm_datetime(dt) == "Wednesday, May 13 2026, 12:00 AM ET"

    def test_default_argument_uses_now(self):
        # Smoke test: default path runs and produces the expected shape.
        out = format_llm_datetime()
        assert " ET" in out
        assert ", " in out
