"""Tests for the auto-resolver registry — CreateLoop, AdvanceStage, LinkThread."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.classifier.models import (
    EmailClassification,
    SuggestedAction,
    Suggestion,
    SuggestionStatus,
)
from api.classifier.resolvers import (
    DEFAULT_CANDIDATE_NAME,
    AdvanceStageResolver,
    CreateLoopResolver,
    LinkThreadResolver,
    ResolverContext,
    build_registry,
    try_auto_resolve,
)
from api.scheduling.models import (
    Candidate,
    Loop,
    StageState,
)


def _ctx(loop_service: MagicMock, suggestion_service: MagicMock, arq_pool=None) -> ResolverContext:
    return ResolverContext(
        coordinator_email="coord@lrp.com",
        gmail_thread_id="thread_1",
        gmail_message_id="msg_1",
        gmail_subject="Interview request",
        loop_service=loop_service,
        suggestion_service=suggestion_service,
        arq_pool=arq_pool,
    )


def _suggestion(
    action: SuggestedAction,
    *,
    suggestion_id: str = "sug_1",
    loop_id: str | None = None,
    action_data: dict | None = None,
) -> Suggestion:
    return Suggestion(
        id=suggestion_id,
        coordinator_email="coord@lrp.com",
        gmail_message_id="msg_1",
        gmail_thread_id="thread_1",
        loop_id=loop_id,
        classification=EmailClassification.NEW_INTERVIEW_REQUEST,
        action=action,
        confidence=0.9,
        summary="test",
        action_data=action_data or {},
        status=SuggestionStatus.PENDING,
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
    )


def _loop(loop_id: str = "lop_1", state: StageState = StageState.NEW) -> Loop:
    return Loop(
        id=loop_id,
        coordinator_id="crd_1",
        candidate_id="can_1",
        title="Round 1",
        state=state,
        created_at=datetime(2026, 4, 10, tzinfo=UTC),
        updated_at=datetime(2026, 4, 10, tzinfo=UTC),
        candidate=Candidate(id="can_1", name="Test", created_at=datetime(2026, 4, 10, tzinfo=UTC)),
    )


class TestCreateLoopResolver:
    @pytest.mark.asyncio
    async def test_full_extraction_creates_loop_with_all_contacts(self):
        loop_service = MagicMock()
        loop_service.find_or_create_client_contact = AsyncMock(return_value=MagicMock(id="cli_1"))
        loop_service.find_or_create_contact = AsyncMock(return_value=MagicMock(id="con_1"))
        loop_service.create_loop = AsyncMock(return_value=_loop())

        suggestion = _suggestion(
            SuggestedAction.CREATE_LOOP,
            action_data={
                "candidate_name": "Claire Thompson",
                "client_name": "Haley",
                "client_email": "haley@acme.com",
                "client_company": "ACME",
                "recruiter_name": "Bob",
                "recruiter_email": "bob@lrp.com",
            },
        )
        ctx = _ctx(loop_service, MagicMock(), arq_pool=AsyncMock())
        await CreateLoopResolver().resolve(suggestion, ctx)

        loop_service.create_loop.assert_awaited_once()
        kwargs = loop_service.create_loop.await_args.kwargs
        assert kwargs["candidate_name"] == "Claire Thompson"
        assert kwargs["client_contact_id"] == "cli_1"
        assert kwargs["recruiter_id"] == "con_1"
        assert kwargs["title"] == "Claire Thompson, ACME"

    @pytest.mark.asyncio
    async def test_empty_extraction_creates_unknown_candidate_with_null_contacts(self):
        loop_service = MagicMock()
        loop_service.find_or_create_client_contact = AsyncMock()
        loop_service.find_or_create_contact = AsyncMock()
        loop_service.create_loop = AsyncMock(return_value=_loop())

        suggestion = _suggestion(SuggestedAction.CREATE_LOOP, action_data={})
        ctx = _ctx(loop_service, MagicMock(), arq_pool=AsyncMock())
        await CreateLoopResolver().resolve(suggestion, ctx)

        loop_service.find_or_create_client_contact.assert_not_called()
        loop_service.find_or_create_contact.assert_not_called()
        kwargs = loop_service.create_loop.await_args.kwargs
        assert kwargs["candidate_name"] == DEFAULT_CANDIDATE_NAME
        assert kwargs["client_contact_id"] is None
        assert kwargs["recruiter_id"] is None
        assert kwargs["title"] == DEFAULT_CANDIDATE_NAME

    @pytest.mark.asyncio
    async def test_enqueues_next_action_after_creation(self):
        loop_service = MagicMock()
        loop_service.find_or_create_client_contact = AsyncMock(return_value=MagicMock(id="cli_1"))
        loop_service.find_or_create_contact = AsyncMock(return_value=MagicMock(id="con_1"))
        loop_service.create_loop = AsyncMock(return_value=_loop())
        arq_pool = AsyncMock()

        suggestion = _suggestion(SuggestedAction.CREATE_LOOP, action_data={"candidate_name": "X"})
        ctx = _ctx(loop_service, MagicMock(), arq_pool=arq_pool)
        await CreateLoopResolver().resolve(suggestion, ctx)

        arq_pool.enqueue_job.assert_awaited_once()
        args = arq_pool.enqueue_job.await_args.args
        assert args[0] == "run_next_action_agent"
        assert args[1] == "coord@lrp.com"
        assert args[2] == "msg_1"
        assert args[3] == "thread_1"


class TestAdvanceStageResolver:
    @pytest.mark.asyncio
    async def test_advances_loop_state_from_action_data(self):
        loop_service = MagicMock()
        loop_service.advance_state = AsyncMock()

        suggestion = _suggestion(
            SuggestedAction.ADVANCE_STAGE,
            loop_id="lop_42",
            action_data={"target_stage": "awaiting_client"},
        )
        ctx = _ctx(loop_service, MagicMock())
        await AdvanceStageResolver().resolve(suggestion, ctx)

        loop_service.advance_state.assert_awaited_once()
        kwargs = loop_service.advance_state.await_args.kwargs
        assert kwargs["loop_id"] == "lop_42"
        assert kwargs["to_state"] == StageState.AWAITING_CLIENT

    @pytest.mark.asyncio
    async def test_skips_when_no_target_stage(self):
        loop_service = MagicMock()
        loop_service.advance_state = AsyncMock()

        suggestion = _suggestion(SuggestedAction.ADVANCE_STAGE, loop_id="lop_42", action_data={})
        ctx = _ctx(loop_service, MagicMock())
        await AdvanceStageResolver().resolve(suggestion, ctx)

        loop_service.advance_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_loop_id(self):
        loop_service = MagicMock()
        loop_service.advance_state = AsyncMock()

        suggestion = _suggestion(
            SuggestedAction.ADVANCE_STAGE, action_data={"target_stage": "scheduled"}
        )
        ctx = _ctx(loop_service, MagicMock())
        await AdvanceStageResolver().resolve(suggestion, ctx)

        loop_service.advance_state.assert_not_called()


class TestLinkThreadResolver:
    @pytest.mark.asyncio
    async def test_links_thread_and_enqueues_next_action(self):
        loop_service = MagicMock()
        loop_service.link_thread = AsyncMock(return_value=MagicMock())
        arq_pool = AsyncMock()

        suggestion = _suggestion(SuggestedAction.LINK_THREAD, loop_id="lop_42")
        ctx = _ctx(loop_service, MagicMock(), arq_pool=arq_pool)
        await LinkThreadResolver().resolve(suggestion, ctx)

        loop_service.link_thread.assert_awaited_once()
        kwargs = loop_service.link_thread.await_args.kwargs
        assert kwargs["loop_id"] == "lop_42"
        assert kwargs["gmail_thread_id"] == "thread_1"
        arq_pool.enqueue_job.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_when_target_loop_id_missing(self):
        loop_service = MagicMock()
        loop_service.link_thread = AsyncMock()

        suggestion = _suggestion(SuggestedAction.LINK_THREAD)  # no loop_id
        ctx = _ctx(loop_service, MagicMock())
        await LinkThreadResolver().resolve(suggestion, ctx)

        loop_service.link_thread.assert_not_called()


class TestRegistry:
    def test_combined_registry_has_three_actions(self):
        registry = build_registry()
        assert SuggestedAction.CREATE_LOOP in registry
        assert SuggestedAction.ADVANCE_STAGE in registry
        assert SuggestedAction.LINK_THREAD in registry
        assert SuggestedAction.DRAFT_EMAIL not in registry


class TestTryAutoResolve:
    @pytest.mark.asyncio
    async def test_marks_suggestion_auto_applied_on_success(self):
        loop_service = MagicMock()
        loop_service.advance_state = AsyncMock()
        suggestion_service = MagicMock()
        suggestion_service.resolve = AsyncMock()

        registry = {SuggestedAction.ADVANCE_STAGE: AdvanceStageResolver()}
        suggestion = _suggestion(
            SuggestedAction.ADVANCE_STAGE,
            loop_id="lop_1",
            action_data={"target_stage": "awaiting_client"},
        )
        ctx = _ctx(loop_service, suggestion_service)

        applied = await try_auto_resolve(suggestion, ctx, registry)
        assert applied is True
        suggestion_service.resolve.assert_awaited_once()
        kwargs = suggestion_service.resolve.await_args.kwargs
        assert kwargs["status"] == SuggestionStatus.AUTO_APPLIED

    @pytest.mark.asyncio
    async def test_returns_false_when_action_not_registered(self):
        suggestion = _suggestion(SuggestedAction.NO_ACTION)
        ctx = _ctx(MagicMock(), MagicMock())
        applied = await try_auto_resolve(suggestion, ctx, build_registry())
        assert applied is False

    @pytest.mark.asyncio
    async def test_returns_false_and_does_not_mark_when_resolver_raises(self):
        loop_service = MagicMock()
        loop_service.advance_state = AsyncMock(side_effect=RuntimeError("boom"))
        suggestion_service = MagicMock()
        suggestion_service.resolve = AsyncMock()

        registry = {SuggestedAction.ADVANCE_STAGE: AdvanceStageResolver()}
        suggestion = _suggestion(
            SuggestedAction.ADVANCE_STAGE,
            loop_id="lop_1",
            action_data={"target_stage": "awaiting_client"},
        )
        ctx = _ctx(loop_service, suggestion_service)

        applied = await try_auto_resolve(suggestion, ctx, registry)
        assert applied is False
        suggestion_service.resolve.assert_not_called()
