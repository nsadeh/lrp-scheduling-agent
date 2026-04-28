"""ClassifierHook — the email classification agent.

Implements the EmailHook protocol. When a new email arrives via the push
pipeline, the classifier:
1. Assembles context (thread history, all linked loops, active loops)
2. Calls the LLM via the classify_email typed endpoint
3. Applies guardrails (action-state validation, confidence thresholds)
4. Persists suggestions to agent_suggestions
5. Auto-resolves CREATE_LOOP / ADVANCE_STAGE / LINK_THREAD via the resolver
   registry (suggestions still emitted for audit; resolver applies the
   side effect and marks the suggestion AUTO_APPLIED so the UI hides it)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from api.classifier.endpoint import ClassifyEmailInput, classify_email
from api.classifier.formatters import (
    format_active_loops,
    format_email,
    format_events,
    format_linked_loops,
    format_stage_states,
    format_thread_history,
    format_transitions,
)
from api.classifier.models import (
    ClassificationResult,
    CreateLoopExtraction,
    SuggestedAction,
    SuggestionItem,
)
from api.classifier.resolvers import (
    ResolverContext,
    build_registry,
    try_auto_resolve,
)
from api.classifier.sender_blacklist import SenderBlacklist
from api.classifier.service import SuggestionService  # noqa: TC001 — used at runtime in __init__
from api.gmail.hooks import EmailEvent, MessageDirection
from api.scheduling.models import ALLOWED_TRANSITIONS, StageState

if TYPE_CHECKING:
    from arq.connections import ArqRedis
    from langfuse import Langfuse

    from api.ai.llm_service import LLMService
    from api.drafts.service import DraftService
    from api.gmail.models import Message
    from api.scheduling.models import Loop, Stage
    from api.scheduling.service import LoopService

logger = logging.getLogger(__name__)

# Guardrail: LINK_THREAD requires >= this confidence
LINK_THREAD_MIN_CONFIDENCE = 0.9

# Rate limit: max classifications per coordinator per hour
MAX_CLASSIFICATIONS_PER_HOUR = 100


# Fields lifted from the loose extracted_entities dict into the typed
# CreateLoopExtraction payload on action_data. Kept in sync with
# classifier/models.py:CreateLoopExtraction.
_CREATE_LOOP_FIELDS = (
    "candidate_name",
    "client_name",
    "client_email",
    "client_company",
    "cm_name",
    "cm_email",
    "recruiter_name",
    "recruiter_email",
)


def _coerce_create_loop_action_data(item: SuggestionItem) -> SuggestionItem:
    """Parallel-write typed CreateLoopExtraction into action_data for CREATE_LOOP.

    The classifier emits CREATE_LOOP fields into extracted_entities (loose
    dict). Downstream readers (overview card, show_create_form handler,
    CreateLoopResolver) prefer action_data over extracted_entities. This
    coercion populates action_data with the same values as a typed
    CreateLoopExtraction dump so both paths see a consistent shape.
    """
    if item.action != SuggestedAction.CREATE_LOOP:
        return item
    if item.action_data:
        # Classifier already produced typed data — trust it.
        return item

    values: dict[str, str | None] = {}
    for field in _CREATE_LOOP_FIELDS:
        raw = item.extracted_entities.get(field)
        values[field] = raw if isinstance(raw, str) and raw else None

    extraction = CreateLoopExtraction(**values)
    return item.model_copy(update={"action_data": extraction.model_dump()})


class ClassifierHook:
    """Email classification agent — implements the EmailHook protocol."""

    def __init__(
        self,
        *,
        llm: LLMService,
        langfuse: Langfuse,
        suggestion_service: SuggestionService,
        loop_service: LoopService,
        draft_service: DraftService | None = None,
        sender_blacklist: SenderBlacklist | None = None,
        arq_pool: ArqRedis | None = None,
    ):
        self._llm = llm
        self._langfuse = langfuse
        self._suggestions = suggestion_service
        self._loops = loop_service
        self._draft_service = draft_service
        # Default to empty when not injected — preserves test compatibility
        # and makes the blacklist a strict opt-in for production wiring.
        self._sender_blacklist = sender_blacklist or SenderBlacklist.empty()
        # Used by auto-resolvers to enqueue follow-up reclassification jobs
        # after CREATE_LOOP / LINK_THREAD. None in tests / API context.
        self._arq_pool = arq_pool
        self._resolver_registry = build_registry()

    async def on_email(self, event: EmailEvent) -> None:
        """Process an email event — classify and persist suggestions."""
        msg = event.message

        # Outgoing emails on unlinked threads: skip entirely
        if event.direction == MessageDirection.OUTGOING:
            linked_loops = await self._loops.find_loops_by_thread(msg.thread_id)
            if not linked_loops:
                logger.debug(
                    "skipping outgoing email on unlinked thread %s",
                    msg.thread_id,
                )
                return
            await self._classify_and_persist(event, linked_loops)
            return

        # Incoming email — check for linked loops, classify either way
        linked_loops = await self._loops.find_loops_by_thread(msg.thread_id)

        # Sender blacklist: skip incoming emails from known non-client senders
        # (newsletters, transactional notifications, cold outreach) when the
        # thread isn't already linked to a scheduling loop. We deliberately
        # do NOT apply the blacklist on linked threads — if a newsletter
        # somehow lands inside an active candidate conversation, we still
        # want the classifier to see it.
        if not linked_loops and self._sender_blacklist.is_blocked(msg.from_.email):
            logger.debug(
                "skipping blacklisted sender %s on unlinked thread %s",
                msg.from_.email,
                msg.thread_id,
            )
            return

        await self._classify_and_persist(event, linked_loops)

    async def _classify_and_persist(
        self,
        event: EmailEvent,
        linked_loops: list[Loop],
    ) -> None:
        """Run the classification pipeline: context → LLM → guardrails → persist → auto-resolve."""
        msg = event.message

        # 1. Assemble context
        context_input = await self._build_context(event, linked_loops, event.thread_messages)

        # 2. Call LLM
        try:
            result: ClassificationResult = await classify_email(
                llm=self._llm,
                langfuse=self._langfuse,
                data=context_input,
            )
        except Exception:
            logger.exception(
                "classification failed for message %s on thread %s",
                msg.id,
                msg.thread_id,
            )
            # Create a NEEDS_ATTENTION suggestion on LLM failure
            await self._suggestions.create_suggestion(
                coordinator_email=event.coordinator_email,
                gmail_message_id=msg.id,
                gmail_thread_id=msg.thread_id,
                item=SuggestionItem(
                    classification="follow_up_needed",
                    action="ask_coordinator",
                    confidence=0.0,
                    summary="Classification failed — please review this email manually.",
                    questions=["The AI classifier encountered an error processing this email."],
                ),
                reasoning="LLM call failed",
                loop_id=linked_loops[0].id if linked_loops else None,
            )
            return

        # 3. Apply guardrails, persist, and auto-resolve each suggestion
        for item in result.suggestions:
            # Resolve which loop (if any) this suggestion targets
            target_loop = self._resolve_target_loop(item, linked_loops)

            # Guardrail: drop loop-scoped suggestions when no target loop is
            # available. CREATE_LOOP is exempt — it creates a loop. Other
            # actions (DRAFT_EMAIL, ADVANCE_STAGE, MARK_COLD, LINK_THREAD)
            # require an existing loop.
            if target_loop is None and item.action in (
                SuggestedAction.DRAFT_EMAIL,
                SuggestedAction.ADVANCE_STAGE,
                SuggestedAction.MARK_COLD,
            ):
                logger.info(
                    "dropping %s suggestion — no matching loop on thread %s",
                    item.action,
                    msg.thread_id,
                )
                continue

            item = self._apply_guardrails(item, target_loop)
            item = _coerce_create_loop_action_data(item)
            suggestion = await self._suggestions.create_suggestion(
                coordinator_email=event.coordinator_email,
                gmail_message_id=msg.id,
                gmail_thread_id=msg.thread_id,
                item=item,
                reasoning=result.reasoning,
                loop_id=target_loop.id if target_loop else None,
                stage_id=self._resolve_stage_id(item, target_loop),
            )

            logger.info(
                "suggestion created: %s (classification=%s, action=%s, confidence=%.2f)",
                suggestion.id,
                item.classification,
                item.action,
                item.confidence,
            )

            # 4. Auto-resolve registered actions (CREATE_LOOP / ADVANCE_STAGE /
            # LINK_THREAD). On success the suggestion is marked AUTO_APPLIED
            # and the overview UI never surfaces it.
            ctx = ResolverContext(
                coordinator_email=event.coordinator_email,
                gmail_thread_id=msg.thread_id,
                gmail_message_id=msg.id,
                gmail_subject=msg.subject,
                loop_service=self._loops,
                suggestion_service=self._suggestions,
                arq_pool=self._arq_pool,
            )
            applied = await try_auto_resolve(suggestion, ctx, self._resolver_registry)
            if applied:
                # Skip draft generation for auto-resolved actions —
                # CREATE_LOOP/LINK_THREAD trigger reclassification which
                # produces follow-up DRAFT_EMAIL on the next pass.
                continue

            # Draft generation for DRAFT_EMAIL actions
            if (
                item.action == SuggestedAction.DRAFT_EMAIL
                and self._draft_service is not None
                and target_loop is not None
            ):
                try:
                    await self._draft_service.generate_draft(
                        suggestion=suggestion,
                        loop=target_loop,
                        thread_messages=event.thread_messages,
                    )
                    logger.info("draft generated for suggestion %s", suggestion.id)
                except Exception:
                    logger.exception("draft generation failed for suggestion %s", suggestion.id)

    def _resolve_target_loop(
        self,
        item: SuggestionItem,
        linked_loops: list[Loop],
    ) -> Loop | None:
        """Pick which loop a suggestion applies to.

        Multi-loop threads (one Gmail thread linked to multiple loops)
        require the LLM to populate `target_loop_id` to disambiguate. When
        there's exactly one linked loop we don't require it. With zero
        linked loops, only CREATE_LOOP can proceed and target_loop is None.
        """
        if item.target_loop_id:
            for loop in linked_loops:
                if loop.id == item.target_loop_id:
                    return loop
            # Mismatch — LLM pointed at a loop not in our linked set. Fall
            # through to single-loop fallback so we don't silently skip the
            # action; if there's only one loop, default to it.
        if len(linked_loops) == 1:
            return linked_loops[0]
        return None

    async def _build_context(
        self,
        event: EmailEvent,
        linked_loops: list[Loop],
        thread_messages: list[Message] | None = None,
    ) -> ClassifyEmailInput:
        """Assemble all context for the LLM call."""
        msg = event.message

        # Format thread history from the worker's thread cache
        if thread_messages:
            thread_history_text = format_thread_history(thread_messages, msg.id)
        else:
            thread_history_text = "No prior messages in this thread."

        # Load active loops for the coordinator (for thread-to-loop matching)
        active_loops: list[Loop] = []
        if not linked_loops:
            coord = await self._loops.get_coordinator_by_email(event.coordinator_email)
            if coord:
                active_loops = await self._get_active_loops(coord.id)

        # Load events for the first linked loop (events are loop-scoped; we
        # don't try to merge across multi-loop threads for now)
        events = []
        if linked_loops:
            events = await self._loops.get_events(linked_loops[0].id)

        return ClassifyEmailInput(
            stage_states=format_stage_states(),
            transitions=format_transitions(),
            email=format_email(msg, event.direction.value, event.message_type.value),
            thread_history=thread_history_text,
            loop_state=format_linked_loops(linked_loops),
            active_loops_summary=format_active_loops(active_loops),
            events=format_events(events),
            direction=event.direction.value,
        )

    async def _get_active_loops(self, coordinator_id: str) -> list[Loop]:
        """Load coordinator's active loops for thread matching context."""
        from api.scheduling.queries import queries as sched_queries

        async with self._loops._pool.connection() as conn:
            rows = []
            async for row in sched_queries.get_loops_for_coordinator(
                conn, coordinator_id=coordinator_id
            ):
                rows.append(row)

        loops = []
        for row in rows:
            loop = await self._loops.get_loop(row[0])
            loops.append(loop)
        return loops

    def _apply_guardrails(
        self,
        item: SuggestionItem,
        target_loop: Loop | None,
    ) -> SuggestionItem:
        """Apply post-LLM guardrails to a suggestion item."""
        # Guardrail: LINK_THREAD confidence floor
        if (
            item.action == SuggestedAction.LINK_THREAD
            and item.confidence < LINK_THREAD_MIN_CONFIDENCE
        ):
            logger.warning(
                "LINK_THREAD confidence %.2f below threshold %.2f — converting to CREATE_LOOP",
                item.confidence,
                LINK_THREAD_MIN_CONFIDENCE,
            )
            return item.model_copy(
                update={
                    "action": SuggestedAction.CREATE_LOOP,
                    "summary": f"{item.summary} (link confidence too low, suggesting new loop)",
                }
            )

        # Guardrail: action-state validation for ADVANCE_STAGE
        if item.action == SuggestedAction.ADVANCE_STAGE and item.target_state and target_loop:
            current_stage = self._find_target_stage(item, target_loop)
            if current_stage:
                allowed = ALLOWED_TRANSITIONS.get(current_stage.state, set())
                if StageState(item.target_state) not in allowed:
                    logger.warning(
                        "invalid transition %s → %s — demoting to ASK_COORDINATOR",
                        current_stage.state,
                        item.target_state,
                    )
                    return item.model_copy(
                        update={
                            "action": SuggestedAction.ASK_COORDINATOR,
                            "questions": [
                                f"Suggested transition from {current_stage.state} to "
                                f"{item.target_state} is not allowed. Please review."
                            ],
                            "summary": f"{item.summary} (invalid state transition)",
                        }
                    )

        return item

    def _find_target_stage(
        self,
        item: SuggestionItem,
        loop: Loop,
    ) -> Stage | None:
        """Find the stage that an ADVANCE_STAGE suggestion targets."""

        # If the item specifies a stage ID, use it
        if item.target_stage_id:
            for stage in loop.stages:
                if stage.id == item.target_stage_id:
                    return stage

        # Otherwise, find the most active stage
        active = [s for s in loop.stages if s.is_active]
        return active[0] if active else (loop.stages[0] if loop.stages else None)

    def _resolve_stage_id(
        self,
        item: SuggestionItem,
        target_loop: Loop | None,
    ) -> str | None:
        """Resolve the stage_id for a suggestion."""
        if item.target_stage_id:
            return item.target_stage_id
        if target_loop and item.action == SuggestedAction.ADVANCE_STAGE:
            stage = self._find_target_stage(item, target_loop)
            return stage.id if stage else None
        return None
