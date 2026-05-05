"""Auto-resolver registry — actions applied without coordinator approval.

Some actions (CREATE_LOOP, ADVANCE_STAGE, LINK_THREAD) are mechanical —
there is no judgment for the coordinator to add. The matching resolver
applies them in the background and marks the suggestion AUTO_APPLIED so the
overview UI never surfaces it.

Two registries serve the two-stage pipeline:
- build_classifier_registry(): CREATE_LOOP, LINK_THREAD (for LoopClassifier)
- build_agent_registry(): ADVANCE_STAGE (for NextActionAgent)

After CREATE_LOOP/LINK_THREAD auto-resolve, enqueue_next_action() fires
the NextActionAgent on the now-linked thread — this prevents the recursion
issue where the old reclassify path could trigger infinite loop creation.

Failure mode (per design): on any exception, capture to Sentry and drop.
The suggestion stays PENDING but is not surfaced — confirmed acceptable
loss for the happy-path optimization.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

import sentry_sdk

from api.classifier.models import (
    CreateLoopExtraction,
    SuggestedAction,
    Suggestion,
    SuggestionStatus,
)
from api.scheduling.models import StageState

if TYPE_CHECKING:
    from arq.connections import ArqRedis

    from api.classifier.service import SuggestionService
    from api.scheduling.service import LoopService

logger = logging.getLogger(__name__)

DEFAULT_CANDIDATE_NAME = "Unknown Candidate"


class ResolverContext:
    """Per-call context handed to resolvers.

    Holds the gmail thread/message ids so resolvers that need to link the
    new loop to the originating thread (CreateLoopResolver) or enqueue
    follow-up reclassification jobs (CreateLoopResolver, LinkThreadResolver)
    have what they need without re-querying.
    """

    def __init__(
        self,
        *,
        coordinator_email: str,
        gmail_thread_id: str,
        gmail_message_id: str,
        gmail_subject: str | None,
        loop_service: LoopService,
        suggestion_service: SuggestionService,
        arq_pool: ArqRedis | None,
    ) -> None:
        self.coordinator_email = coordinator_email
        self.gmail_thread_id = gmail_thread_id
        self.gmail_message_id = gmail_message_id
        self.gmail_subject = gmail_subject
        self.loops = loop_service
        self.suggestions = suggestion_service
        self.arq_pool = arq_pool

    async def enqueue_next_action(self) -> None:
        """Fire the NextActionAgent on this thread after a loop was created or linked.

        The thread is now linked to a loop, so the agent will produce
        follow-up suggestions (DRAFT_EMAIL, ADVANCE_STAGE) that weren't
        possible before. Unlike the old reclassify path, this cannot
        trigger loop creation recursion — the agent blacklists CREATE_LOOP.
        """
        if self.arq_pool is None:
            logger.warning(
                "no arq_pool — skipping next action enqueue for thread %s",
                self.gmail_thread_id,
            )
            return
        try:
            await self.arq_pool.enqueue_job(
                "run_next_action_agent",
                self.coordinator_email,
                self.gmail_message_id,
                self.gmail_thread_id,
            )
        except Exception:
            logger.exception(
                "failed to enqueue next action agent for thread %s",
                self.gmail_thread_id,
            )


class Resolver(Protocol):
    async def resolve(self, suggestion: Suggestion, ctx: ResolverContext) -> None: ...


# ---------------------------------------------------------------------------
# CREATE_LOOP
# ---------------------------------------------------------------------------


class CreateLoopResolver:
    """Auto-create a loop from extracted entities.

    Tolerates missing recruiter/client info — the loop is created with null
    FKs and the missing pieces are collected JIT by the draft widget that
    needs them. Defaults `candidate_name` to "Unknown Candidate" when the
    classifier didn't extract one; the coordinator can rename inline from
    the loop card.
    """

    async def resolve(self, suggestion: Suggestion, ctx: ResolverContext) -> None:
        extraction = self._read_extraction(suggestion)

        candidate_name = (extraction.candidate_name or "").strip() or DEFAULT_CANDIDATE_NAME

        client_contact_id: str | None = None
        if extraction.client_email:
            client_contact = await ctx.loops.find_or_create_client_contact(
                name=(extraction.client_name or "").strip() or extraction.client_email,
                email=extraction.client_email,
                company=(extraction.client_company or None),
            )
            client_contact_id = client_contact.id

        recruiter_id: str | None = None
        if extraction.recruiter_email:
            recruiter = await ctx.loops.find_or_create_contact(
                name=(extraction.recruiter_name or "").strip() or extraction.recruiter_email,
                email=extraction.recruiter_email,
                role="recruiter",
            )
            recruiter_id = recruiter.id

        client_manager_id: str | None = None
        if extraction.cm_email:
            cm = await ctx.loops.find_or_create_contact(
                name=(extraction.cm_name or "").strip() or extraction.cm_email,
                email=extraction.cm_email,
                role="client_manager",
            )
            client_manager_id = cm.id

        title = self._build_title(candidate_name, extraction.client_company)

        loop = await ctx.loops.create_loop(
            coordinator_email=ctx.coordinator_email,
            coordinator_name=ctx.coordinator_email.split("@")[0],
            candidate_name=candidate_name,
            client_contact_id=client_contact_id,
            recruiter_id=recruiter_id,
            title=title,
            client_manager_id=client_manager_id,
            gmail_thread_id=ctx.gmail_thread_id,
            gmail_subject=ctx.gmail_subject,
        )
        logger.info(
            "auto-created loop %s for thread %s (recruiter=%s, client=%s, candidate=%r)",
            loop.id,
            ctx.gmail_thread_id,
            recruiter_id,
            client_contact_id,
            candidate_name,
        )

        await ctx.enqueue_next_action()

    def _read_extraction(self, suggestion: Suggestion) -> CreateLoopExtraction:
        if not suggestion.action_data:
            return CreateLoopExtraction()
        try:
            return CreateLoopExtraction.model_validate(suggestion.action_data)
        except Exception:
            logger.warning(
                "could not parse action_data as CreateLoopExtraction for suggestion %s",
                suggestion.id,
            )
            return CreateLoopExtraction()

    @staticmethod
    def _build_title(candidate_name: str, company: str | None) -> str:
        if company:
            return f"{candidate_name}, {company}"
        return candidate_name


# ---------------------------------------------------------------------------
# ADVANCE_STAGE
# ---------------------------------------------------------------------------


class AdvanceStageResolver:
    """Advance a loop's state to a new value.

    Reads the target state name from `suggestion.action_data["target_stage"]`
    (populated by the agent's LLM output) and updates the loop directly. The
    resolver name is kept (not "AdvanceStateResolver") because the action
    enum value is still SuggestedAction.ADVANCE_STAGE.
    """

    async def resolve(self, suggestion: Suggestion, ctx: ResolverContext) -> None:
        if not suggestion.loop_id:
            logger.warning(
                "ADVANCE_STAGE suggestion %s missing loop_id — skipping",
                suggestion.id,
            )
            return

        target_stage_name = (suggestion.action_data or {}).get("target_stage")
        if not target_stage_name:
            logger.warning(
                "ADVANCE_STAGE suggestion %s missing action_data.target_stage — skipping",
                suggestion.id,
            )
            return

        await ctx.loops.advance_state(
            loop_id=suggestion.loop_id,
            to_state=StageState(target_stage_name),
            coordinator_email=ctx.coordinator_email,
            triggered_by=f"auto:{suggestion.id}",
        )
        logger.info(
            "auto-advanced loop %s -> %s",
            suggestion.loop_id,
            target_stage_name,
        )


# ---------------------------------------------------------------------------
# LINK_THREAD
# ---------------------------------------------------------------------------


class LinkThreadResolver:
    """Link a Gmail thread to an existing loop the LLM matched it to.

    Confidence floor (0.9) is enforced upstream in
    `LoopClassifier._apply_guardrails`; any LINK_THREAD that reaches the
    resolver has already cleared it. After linking, enqueue the
    NextActionAgent so it can produce follow-up suggestions for the
    now-linked loop.
    """

    async def resolve(self, suggestion: Suggestion, ctx: ResolverContext) -> None:
        target_loop_id = suggestion.loop_id
        if not target_loop_id:
            logger.warning(
                "LINK_THREAD suggestion %s missing target_loop_id — skipping",
                suggestion.id,
            )
            return

        result = await ctx.loops.link_thread(
            loop_id=target_loop_id,
            gmail_thread_id=ctx.gmail_thread_id,
            subject=ctx.gmail_subject,
            coordinator_email=ctx.coordinator_email,
        )
        logger.info(
            "auto-linked thread %s to loop %s (already_linked=%s)",
            ctx.gmail_thread_id,
            target_loop_id,
            result is None,
        )

        await ctx.enqueue_next_action()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def build_classifier_registry() -> dict[SuggestedAction, Resolver]:
    """Auto-resolvers for the LoopClassifier (unlinked threads)."""
    return {
        SuggestedAction.CREATE_LOOP: CreateLoopResolver(),
        SuggestedAction.LINK_THREAD: LinkThreadResolver(),
    }


def build_agent_registry() -> dict[SuggestedAction, Resolver]:
    """Auto-resolvers for the NextActionAgent (linked threads)."""
    return {
        SuggestedAction.ADVANCE_STAGE: AdvanceStageResolver(),
    }


def build_registry() -> dict[SuggestedAction, Resolver]:
    """Combined registry — kept for backward compatibility."""
    return {**build_classifier_registry(), **build_agent_registry()}


async def try_auto_resolve(
    suggestion: Suggestion,
    ctx: ResolverContext,
    registry: dict[SuggestedAction, Resolver],
) -> bool:
    """Attempt to auto-resolve a suggestion.

    Returns True on success (suggestion marked AUTO_APPLIED), False if no
    resolver is registered or the resolver raised. On exception, captures
    to Sentry and drops — the suggestion stays PENDING but the UI filters
    it out via the dispatcher in overview/cards.py.
    """
    resolver = registry.get(suggestion.action)
    if resolver is None:
        return False

    try:
        await resolver.resolve(suggestion, ctx)
    except Exception as exc:
        logger.exception(
            "auto-resolver failed for suggestion %s (action=%s)",
            suggestion.id,
            suggestion.action,
        )
        sentry_sdk.capture_exception(exc)
        return False

    try:
        await ctx.suggestions.resolve(
            suggestion.id,
            status=SuggestionStatus.AUTO_APPLIED,
            resolved_by="agent",
        )
    except Exception as exc:
        logger.exception(
            "failed to mark suggestion %s as AUTO_APPLIED — side effects already applied",
            suggestion.id,
        )
        sentry_sdk.capture_exception(exc)
        return False

    return True
