"""Tests for the auto-resolver registry — CreateLoop, AdvanceStage, LinkThread, NoAction."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.classifier.models import (
    EmailClassification,
    SuggestedAction,
    Suggestion,
    SuggestionStatus,
)
from api.classifier.resolvers import (
    AdvanceStageResolver,
    CreateLoopResolver,
    LinkThreadResolver,
    NoActionResolver,
    ResolverContext,
    build_agent_registry,
    build_classifier_registry,
    build_registry,
    try_auto_resolve,
)
from api.encore import (
    AmbiguousRecruiters,
    EncoreLookupError,
    NoMatch,
    RecruiterCandidate,
    Skipped,
    UniqueRecruiter,
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
    async def test_empty_extraction_rejects_unknown_candidate(self):
        loop_service = MagicMock()
        loop_service.create_loop = AsyncMock(return_value=_loop())

        suggestion = _suggestion(SuggestedAction.CREATE_LOOP, action_data={})
        ctx = _ctx(loop_service, MagicMock(), arq_pool=AsyncMock())
        with pytest.raises(ValueError, match="candidate_name must be a real name"):
            await CreateLoopResolver().resolve(suggestion, ctx)

        loop_service.create_loop.assert_not_called()

    @pytest.mark.asyncio
    async def test_enqueues_next_action_after_creation(self):
        loop_service = MagicMock()
        loop_service.find_or_create_client_contact = AsyncMock(return_value=MagicMock(id="cli_1"))
        loop_service.find_or_create_contact = AsyncMock(return_value=MagicMock(id="con_1"))
        loop_service.create_loop = AsyncMock(return_value=_loop())
        loop_service.set_recruiter = AsyncMock()
        arq_pool = AsyncMock()

        suggestion = _suggestion(
            SuggestedAction.CREATE_LOOP,
            action_data={"candidate_name": "X Y"},
        )
        ctx = _ctx(loop_service, MagicMock(), arq_pool=arq_pool)
        stub = AsyncMock(return_value=Skipped(reason="single_word_name"))
        with patch("api.classifier.resolvers.resolve_recruiter", new=stub):
            await CreateLoopResolver().resolve(suggestion, ctx)

        arq_pool.enqueue_job.assert_awaited_once()
        args = arq_pool.enqueue_job.await_args.args
        assert args[0] == "run_next_action_agent"
        assert args[1] == "coord@lrp.com"
        assert args[2] == "msg_1"
        assert args[3] == "thread_1"


class TestCreateLoopResolverEncoreWiring:
    """Phase 3: dispatch on resolve_recruiter outcomes, with ordering invariant."""

    def _common_mocks(self):
        loop_service = MagicMock()
        loop_service.find_or_create_client_contact = AsyncMock(return_value=MagicMock(id="cli_1"))
        loop_service.find_or_create_contact = AsyncMock(return_value=MagicMock(id="rec_1"))
        loop_service.create_loop = AsyncMock(return_value=_loop(loop_id="lop_99"))
        loop_service.set_recruiter = AsyncMock()
        suggestion_service = MagicMock()
        suggestion_service.create_suggestion = AsyncMock()
        return loop_service, suggestion_service

    @pytest.mark.asyncio
    async def test_unique_outcome_sets_recruiter(self):
        loop_service, suggestion_service = self._common_mocks()
        outcome = UniqueRecruiter(email="dchen@lrp.com", display_name="Dana Chen", source="genie")
        suggestion = _suggestion(
            SuggestedAction.CREATE_LOOP,
            action_data={"candidate_name": "Daniel Kim"},
        )
        ctx = _ctx(loop_service, suggestion_service, arq_pool=AsyncMock())

        with patch(
            "api.classifier.resolvers.resolve_recruiter",
            new=AsyncMock(return_value=outcome),
        ):
            await CreateLoopResolver().resolve(suggestion, ctx)

        loop_service.find_or_create_contact.assert_awaited_once_with(
            name="Dana Chen", email="dchen@lrp.com", role="recruiter"
        )
        loop_service.set_recruiter.assert_awaited_once_with(
            loop_id="lop_99",
            recruiter_id="rec_1",
            coordinator_email="coord@lrp.com",
        )
        suggestion_service.create_suggestion.assert_not_called()

    @pytest.mark.asyncio
    async def test_ambiguous_outcome_emits_update_actor_with_summary(self):
        loop_service, suggestion_service = self._common_mocks()
        outcome = AmbiguousRecruiters(
            candidates=[
                RecruiterCandidate(
                    email="a@lrp.com",
                    display_name="Alice Andrews",
                    last_activity=datetime(2026, 2, 10, tzinfo=UTC),
                    genie_type="Submit to Client",
                ),
                RecruiterCandidate(
                    email="b@lrp.com",
                    display_name="Bob Boyle",
                    last_activity=datetime(2026, 1, 4, tzinfo=UTC),
                    genie_type="General Information",
                ),
            ]
        )
        suggestion = _suggestion(
            SuggestedAction.CREATE_LOOP,
            action_data={"candidate_name": "Daniel Kim"},
        )
        ctx = _ctx(loop_service, suggestion_service, arq_pool=AsyncMock())

        with patch(
            "api.classifier.resolvers.resolve_recruiter",
            new=AsyncMock(return_value=outcome),
        ):
            await CreateLoopResolver().resolve(suggestion, ctx)

        loop_service.set_recruiter.assert_not_called()
        suggestion_service.create_suggestion.assert_awaited_once()
        kwargs = suggestion_service.create_suggestion.await_args.kwargs
        assert kwargs["loop_id"] == "lop_99"
        item = kwargs["item"]
        assert item.action == SuggestedAction.UPDATE_ACTOR
        assert item.action_data == {"role": "recruiter"}
        assert "Daniel Kim" in item.summary
        assert "Alice Andrews" in item.summary
        assert "Bob Boyle" in item.summary
        assert "2026-02-10" in item.summary

    @pytest.mark.asyncio
    async def test_no_match_outcome_emits_short_update_actor(self):
        loop_service, suggestion_service = self._common_mocks()
        outcome = NoMatch(reason="no_genie_rows")
        suggestion = _suggestion(
            SuggestedAction.CREATE_LOOP,
            action_data={"candidate_name": "Phantom Person"},
        )
        ctx = _ctx(loop_service, suggestion_service, arq_pool=AsyncMock())

        with patch(
            "api.classifier.resolvers.resolve_recruiter",
            new=AsyncMock(return_value=outcome),
        ):
            await CreateLoopResolver().resolve(suggestion, ctx)

        suggestion_service.create_suggestion.assert_awaited_once()
        item = suggestion_service.create_suggestion.await_args.kwargs["item"]
        assert item.action == SuggestedAction.UPDATE_ACTOR
        assert "no_genie_rows" in item.summary
        assert "Phantom Person" in item.summary

    @pytest.mark.asyncio
    async def test_lookup_error_outcome_emits_update_actor(self):
        loop_service, suggestion_service = self._common_mocks()
        outcome = EncoreLookupError(exception_type="OperationalError")
        suggestion = _suggestion(
            SuggestedAction.CREATE_LOOP,
            action_data={"candidate_name": "Daniel Kim"},
        )
        ctx = _ctx(loop_service, suggestion_service, arq_pool=AsyncMock())

        with patch(
            "api.classifier.resolvers.resolve_recruiter",
            new=AsyncMock(return_value=outcome),
        ):
            await CreateLoopResolver().resolve(suggestion, ctx)

        suggestion_service.create_suggestion.assert_awaited_once()
        item = suggestion_service.create_suggestion.await_args.kwargs["item"]
        assert item.action == SuggestedAction.UPDATE_ACTOR
        assert "OperationalError" in item.summary

    @pytest.mark.asyncio
    async def test_skipped_outcome_is_a_noop(self):
        loop_service, suggestion_service = self._common_mocks()
        outcome = Skipped(reason="coordinator_is_adam")
        suggestion = _suggestion(
            SuggestedAction.CREATE_LOOP,
            action_data={"candidate_name": "Daniel Kim"},
        )
        ctx = _ctx(loop_service, suggestion_service, arq_pool=AsyncMock())

        with patch(
            "api.classifier.resolvers.resolve_recruiter",
            new=AsyncMock(return_value=outcome),
        ):
            await CreateLoopResolver().resolve(suggestion, ctx)

        loop_service.set_recruiter.assert_not_called()
        suggestion_service.create_suggestion.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolver_skipped_when_llm_supplied_recruiter_email(self):
        loop_service, suggestion_service = self._common_mocks()
        suggestion = _suggestion(
            SuggestedAction.CREATE_LOOP,
            action_data={
                "candidate_name": "Daniel Kim",
                "recruiter_email": "preset@lrp.com",
                "recruiter_name": "Preset Recruiter",
            },
        )
        ctx = _ctx(loop_service, suggestion_service, arq_pool=AsyncMock())

        spy = AsyncMock(return_value=Skipped(reason="coordinator_is_adam"))
        with patch("api.classifier.resolvers.resolve_recruiter", new=spy):
            await CreateLoopResolver().resolve(suggestion, ctx)

        spy.assert_not_called()
        loop_service.set_recruiter.assert_not_called()
        suggestion_service.create_suggestion.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolver_runs_before_enqueue_next_action(self):
        """Ordering invariant: any side effect from the resolver must complete
        BEFORE the next-action agent is enqueued, so the agent observes a
        fully resolved loop (recruiter set or pending UPDATE_ACTOR)."""
        loop_service, suggestion_service = self._common_mocks()
        arq_pool = AsyncMock()

        # Share a parent mock so call order across services is recorded together.
        parent = MagicMock()
        parent.attach_mock(loop_service.set_recruiter, "set_recruiter")
        parent.attach_mock(arq_pool.enqueue_job, "enqueue_job")

        outcome = UniqueRecruiter(email="dchen@lrp.com", display_name="Dana Chen", source="genie")
        suggestion = _suggestion(
            SuggestedAction.CREATE_LOOP,
            action_data={"candidate_name": "Daniel Kim"},
        )
        ctx = _ctx(loop_service, suggestion_service, arq_pool=arq_pool)

        with patch(
            "api.classifier.resolvers.resolve_recruiter",
            new=AsyncMock(return_value=outcome),
        ):
            await CreateLoopResolver().resolve(suggestion, ctx)

        call_names = [c[0] for c in parent.mock_calls]
        assert call_names.index("set_recruiter") < call_names.index("enqueue_job"), (
            f"Resolver-emitted side effect must come before enqueue_next_action; "
            f"got order: {call_names}"
        )

    @pytest.mark.asyncio
    async def test_resolver_emit_runs_before_enqueue_on_ambiguous(self):
        loop_service, suggestion_service = self._common_mocks()
        arq_pool = AsyncMock()

        parent = MagicMock()
        parent.attach_mock(suggestion_service.create_suggestion, "create_suggestion")
        parent.attach_mock(arq_pool.enqueue_job, "enqueue_job")

        outcome = AmbiguousRecruiters(
            candidates=[
                RecruiterCandidate(
                    email="a@lrp.com",
                    display_name="A",
                    last_activity=datetime(2026, 2, 10, tzinfo=UTC),
                    genie_type="General Information",
                ),
                RecruiterCandidate(
                    email="b@lrp.com",
                    display_name="B",
                    last_activity=datetime(2026, 1, 1, tzinfo=UTC),
                    genie_type="General Information",
                ),
            ]
        )
        suggestion = _suggestion(
            SuggestedAction.CREATE_LOOP,
            action_data={"candidate_name": "Daniel Kim"},
        )
        ctx = _ctx(loop_service, suggestion_service, arq_pool=arq_pool)

        with patch(
            "api.classifier.resolvers.resolve_recruiter",
            new=AsyncMock(return_value=outcome),
        ):
            await CreateLoopResolver().resolve(suggestion, ctx)

        call_names = [c[0] for c in parent.mock_calls]
        assert call_names.index("create_suggestion") < call_names.index(
            "enqueue_job"
        ), f"UPDATE_ACTOR emit must precede enqueue_next_action; got: {call_names}"


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


class TestNoActionResolver:
    @pytest.mark.asyncio
    async def test_resolve_is_a_noop(self):
        loop_service = MagicMock()
        loop_service.advance_state = AsyncMock()
        loop_service.create_loop = AsyncMock()
        loop_service.link_thread = AsyncMock()
        suggestion_service = MagicMock()
        suggestion_service.expire_by_id = AsyncMock()

        suggestion = _suggestion(SuggestedAction.NO_ACTION)
        ctx = _ctx(loop_service, suggestion_service, arq_pool=AsyncMock())
        await NoActionResolver().resolve(suggestion, ctx)

        loop_service.advance_state.assert_not_called()
        loop_service.create_loop.assert_not_called()
        loop_service.link_thread.assert_not_called()
        suggestion_service.expire_by_id.assert_not_called()


class TestRegistry:
    def test_combined_registry_has_expected_actions(self):
        registry = build_registry()
        assert SuggestedAction.CREATE_LOOP in registry
        assert SuggestedAction.ADVANCE_STAGE in registry
        assert SuggestedAction.LINK_THREAD in registry
        assert SuggestedAction.NO_ACTION in registry
        assert SuggestedAction.DRAFT_EMAIL not in registry

    def test_no_action_registered_in_both_registries(self):
        assert SuggestedAction.NO_ACTION in build_classifier_registry()
        assert SuggestedAction.NO_ACTION in build_agent_registry()


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
    async def test_no_action_is_marked_auto_applied(self):
        suggestion_service = MagicMock()
        suggestion_service.resolve = AsyncMock()
        suggestion = _suggestion(SuggestedAction.NO_ACTION)
        ctx = _ctx(MagicMock(), suggestion_service)

        applied = await try_auto_resolve(suggestion, ctx, build_agent_registry())

        assert applied is True
        suggestion_service.resolve.assert_awaited_once()
        call = suggestion_service.resolve.await_args
        assert call.args[0] == "sug_1"
        assert call.kwargs["status"] == SuggestionStatus.AUTO_APPLIED
        assert call.kwargs["resolved_by"] == "agent"

    @pytest.mark.asyncio
    async def test_returns_false_when_action_not_registered(self):
        suggestion = _suggestion(SuggestedAction.DRAFT_EMAIL)
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
