"""Tests for DraftService — recipient routing, draft generation, lifecycle."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.classifier.models import EmailClassification, Suggestion, SuggestionStatus
from api.drafts.models import DraftOutput, DraftStatus
from api.drafts.service import DraftService, _row_to_draft, resolve_recipients
from api.scheduling.models import (
    Candidate,
    ClientContact,
    Contact,
    Coordinator,
    Loop,
    Stage,
    StageState,
)

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _loop(
    loop_id="lop_1",
    stage_state=StageState.AWAITING_CANDIDATE,
    with_client_manager=False,
) -> Loop:
    stages = [
        Stage(
            id="stg_1",
            loop_id=loop_id,
            name="Round 1",
            state=stage_state,
            ordinal=1,
            created_at=datetime(2026, 4, 15, tzinfo=UTC),
            updated_at=datetime(2026, 4, 15, tzinfo=UTC),
        )
    ]
    return Loop(
        id=loop_id,
        coordinator_id="crd_1",
        client_contact_id="cli_1",
        recruiter_id="con_1",
        candidate_id="can_1",
        title="Round 1 - John Smith",
        notes=None,
        created_at=datetime(2026, 4, 15, tzinfo=UTC),
        updated_at=datetime(2026, 4, 15, tzinfo=UTC),
        coordinator=Coordinator(
            id="crd_1",
            name="Fiona",
            email="fiona@lrp.com",
            created_at=datetime(2026, 4, 15, tzinfo=UTC),
        ),
        client_contact=ClientContact(
            id="cli_1",
            name="Haley",
            email="haley@client.com",
            company="Hedge Fund Co",
            created_at=datetime(2026, 4, 15, tzinfo=UTC),
        ),
        recruiter=Contact(
            id="con_1",
            name="Mike",
            email="mike@recruiter.com",
            role="recruiter",
            company=None,
            created_at=datetime(2026, 4, 15, tzinfo=UTC),
        ),
        client_manager=Contact(
            id="con_2",
            name="Sarah",
            email="sarah@lrp.com",
            role="client_manager",
            company=None,
            created_at=datetime(2026, 4, 15, tzinfo=UTC),
        )
        if with_client_manager
        else None,
        candidate=Candidate(
            id="can_1",
            name="John Smith",
            notes=None,
            created_at=datetime(2026, 4, 15, tzinfo=UTC),
        ),
        stages=stages,
        email_threads=[],
    )


def _suggestion(
    sug_id="sug_1",
    classification=EmailClassification.AVAILABILITY_RESPONSE,
    stage_id="stg_1",
) -> Suggestion:
    return Suggestion(
        id=sug_id,
        coordinator_email="fiona@lrp.com",
        gmail_message_id="msg_1",
        gmail_thread_id="thread_1",
        loop_id="lop_1",
        stage_id=stage_id,
        classification=classification,
        action="draft_email",
        auto_advance=False,
        confidence=0.95,
        summary="Share availability with client",
        target_state=None,
        extracted_entities={"availability": "Mon 3/2: 8am-11am"},
        questions=[],
        reasoning="Availability response detected",
        status=SuggestionStatus.PENDING,
        resolved_at=None,
        resolved_by=None,
        created_at=datetime(2026, 4, 15, tzinfo=UTC),
    )


# Fake DB row as dict (psycopg dict_row format)
def _draft_row(
    draft_id="drf_1",
    body="Hi Haley, John is available (in ET): Mon 3/2: 8am-11am.",
):
    return {
        "id": draft_id,
        "suggestion_id": "sug_1",
        "loop_id": "lop_1",
        "stage_id": "stg_1",
        "coordinator_email": "fiona@lrp.com",
        "to_emails": ["haley@client.com"],
        "cc_emails": [],
        "subject": "Re: Round 1 - John Smith",
        "body": body,
        "gmail_thread_id": None,
        "status": "generated",
        "sent_at": None,
        "created_at": datetime(2026, 4, 15, tzinfo=UTC),
        "updated_at": datetime(2026, 4, 15, tzinfo=UTC),
    }


# ---------------------------------------------------------------------------
# _row_to_draft
# ---------------------------------------------------------------------------


class TestRowToDraft:
    def test_converts_dict_to_model(self):
        draft = _row_to_draft(_draft_row())
        assert draft.id == "drf_1"
        assert draft.suggestion_id == "sug_1"
        assert draft.to_emails == ["haley@client.com"]
        assert draft.status == DraftStatus.GENERATED
        assert "John is available" in draft.body


# ---------------------------------------------------------------------------
# Recipient routing
# ---------------------------------------------------------------------------


class TestResolveRecipients:
    """Tests for the module-level resolve_recipients() — single source of truth."""

    def test_new_stage_routes_to_recruiter(self):
        loop = _loop(stage_state=StageState.NEW)
        to, cc = resolve_recipients(loop, loop.stages[0])
        assert to == ["mike@recruiter.com"]
        assert cc == []

    def test_awaiting_candidate_routes_to_client(self):
        loop = _loop(stage_state=StageState.AWAITING_CANDIDATE)
        to, cc = resolve_recipients(loop, loop.stages[0])
        assert to == ["haley@client.com"]
        assert cc == []

    def test_awaiting_client_routes_to_client(self):
        loop = _loop(stage_state=StageState.AWAITING_CLIENT)
        to, cc = resolve_recipients(loop, loop.stages[0])
        assert to == ["haley@client.com"]

    def test_scheduled_routes_to_client(self):
        loop = _loop(stage_state=StageState.SCHEDULED)
        to, cc = resolve_recipients(loop, loop.stages[0])
        assert to == ["haley@client.com"]

    def test_client_manager_cc_when_present(self):
        loop = _loop(stage_state=StageState.AWAITING_CANDIDATE, with_client_manager=True)
        to, cc = resolve_recipients(loop, loop.stages[0])
        assert to == ["haley@client.com"]
        assert cc == ["sarah@lrp.com"]

    def test_none_stage_defaults_to_new(self):
        loop = _loop(stage_state=StageState.NEW)
        to, cc = resolve_recipients(loop, None)
        assert to == ["mike@recruiter.com"]


# ---------------------------------------------------------------------------
# _resolve_stage
# ---------------------------------------------------------------------------


class TestResolveStage:
    def test_finds_stage_by_id(self):
        loop = _loop()
        svc = DraftService(db_pool=MagicMock(), loop_service=MagicMock())
        stage = svc._resolve_stage(loop, "stg_1")
        assert stage is not None
        assert stage.id == "stg_1"

    def test_returns_none_for_unknown_id(self):
        loop = _loop()
        svc = DraftService(db_pool=MagicMock(), loop_service=MagicMock())
        stage = svc._resolve_stage(loop, "stg_nonexistent")
        # Falls back to most_urgent_stage
        assert stage is not None

    def test_none_stage_id_returns_most_urgent(self):
        loop = _loop()
        svc = DraftService(db_pool=MagicMock(), loop_service=MagicMock())
        stage = svc._resolve_stage(loop, None)
        assert stage is not None


# ---------------------------------------------------------------------------
# generate_draft
# ---------------------------------------------------------------------------


class _AsyncCtx:
    """Minimal async context manager for mocking `async with`."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        return False


def _mock_pool():
    """Create an AsyncConnectionPool mock with async context manager support."""
    mock_conn = MagicMock()  # MagicMock so .transaction() doesn't become a coroutine
    mock_pool = MagicMock()
    # pool.connection() returns an async context manager yielding mock_conn
    mock_pool.connection.return_value = _AsyncCtx(mock_conn)
    # conn.transaction() returns an async context manager yielding None
    mock_conn.transaction.return_value = _AsyncCtx(None)
    return mock_pool, mock_conn


class TestGenerateDraft:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        """LLM generates body, draft is persisted."""
        mock_pool, _mock_conn = _mock_pool()

        svc = DraftService(
            db_pool=mock_pool,
            loop_service=MagicMock(),
            llm=MagicMock(),
            langfuse=MagicMock(),
        )

        loop = _loop(stage_state=StageState.AWAITING_CANDIDATE)
        suggestion = _suggestion()

        draft_output = DraftOutput(
            body="Hi Haley, John is available (in ET): Mon 3/2: 8am-11am.",
            reasoning="Sharing availability with client",
        )

        with (
            patch(
                "api.drafts.service.generate_draft_content",
                new_callable=AsyncMock,
                return_value=draft_output,
            ) as mock_endpoint,
            patch("api.drafts.service.queries") as mock_queries,
        ):
            mock_queries.create_draft = AsyncMock(return_value=_draft_row(body=draft_output.body))

            draft = await svc.generate_draft(
                suggestion=suggestion,
                loop=loop,
            )

            assert draft.body == draft_output.body
            assert draft.to_emails == ["haley@client.com"]
            mock_endpoint.assert_awaited_once()
            mock_queries.create_draft.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_llm_failure_creates_empty_draft(self):
        """When LLM fails, draft is created with empty body."""
        mock_pool, _ = _mock_pool()

        svc = DraftService(
            db_pool=mock_pool,
            loop_service=MagicMock(),
            llm=MagicMock(),
            langfuse=MagicMock(),
        )

        loop = _loop(stage_state=StageState.AWAITING_CANDIDATE)
        suggestion = _suggestion()

        with (
            patch(
                "api.drafts.service.generate_draft_content",
                new_callable=AsyncMock,
                side_effect=RuntimeError("LLM timeout"),
            ),
            patch("api.drafts.service.queries") as mock_queries,
        ):
            mock_queries.create_draft = AsyncMock(return_value=_draft_row(body=""))

            draft = await svc.generate_draft(
                suggestion=suggestion,
                loop=loop,
            )

            assert draft.body == ""
            mock_queries.create_draft.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_llm_creates_empty_draft(self):
        """When LLM is not configured, draft is created with empty body."""
        mock_pool, _ = _mock_pool()

        svc = DraftService(
            db_pool=mock_pool,
            loop_service=MagicMock(),
            llm=None,
            langfuse=None,
        )

        loop = _loop(stage_state=StageState.AWAITING_CANDIDATE)
        suggestion = _suggestion()

        with patch("api.drafts.service.queries") as mock_queries:
            mock_queries.create_draft = AsyncMock(return_value=_draft_row(body=""))

            draft = await svc.generate_draft(
                suggestion=suggestion,
                loop=loop,
            )

            assert draft.body == ""


# ---------------------------------------------------------------------------
# Lifecycle methods
# ---------------------------------------------------------------------------


class TestDraftLifecycle:
    @pytest.mark.asyncio
    async def test_mark_sent(self):
        mock_pool, mock_conn = _mock_pool()
        svc = DraftService(db_pool=mock_pool, loop_service=MagicMock())

        with patch("api.drafts.service.queries") as mock_queries:
            mock_queries.mark_draft_sent = AsyncMock()
            await svc.mark_sent("drf_1")
            mock_queries.mark_draft_sent.assert_awaited_once_with(mock_conn, id="drf_1")

    @pytest.mark.asyncio
    async def test_mark_discarded(self):
        mock_pool, mock_conn = _mock_pool()
        svc = DraftService(db_pool=mock_pool, loop_service=MagicMock())

        with patch("api.drafts.service.queries") as mock_queries:
            mock_queries.mark_draft_discarded = AsyncMock()
            await svc.mark_discarded("drf_1")
            mock_queries.mark_draft_discarded.assert_awaited_once_with(mock_conn, id="drf_1")

    @pytest.mark.asyncio
    async def test_update_draft_body(self):
        mock_pool, mock_conn = _mock_pool()
        svc = DraftService(db_pool=mock_pool, loop_service=MagicMock())

        with patch("api.drafts.service.queries") as mock_queries:
            mock_queries.update_draft_body = AsyncMock()
            await svc.update_draft_body("drf_1", "Updated body")
            mock_queries.update_draft_body.assert_awaited_once_with(
                mock_conn, id="drf_1", body="Updated body"
            )
