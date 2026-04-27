"""ClassifierHook — the email classification agent.

Implements the EmailHook protocol. When a new email arrives via the push
pipeline, the classifier:
1. Assembles context (thread history, linked loop, active loops)
2. Calls the LLM via the classify_email typed endpoint
3. Applies guardrails (action-state validation, confidence thresholds)
4. Persists suggestions to agent_suggestions
5. For outgoing emails on loop threads: auto-advances state and supersedes stale suggestions
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from api.classifier.endpoint import ClassifyEmailInput, classify_email
from api.classifier.formatters import (
    format_active_loops,
    format_email,
    format_events,
    format_loop_state,
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
from api.classifier.sender_blacklist import SenderBlacklist
from api.classifier.service import SuggestionService  # noqa: TC001 — used at runtime in __init__
from api.gmail.hooks import EmailEvent, MessageDirection
from api.scheduling.models import ALLOWED_TRANSITIONS, StageState

if TYPE_CHECKING:
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
    dict). Downstream readers (overview card, show_create_form handler)
    prefer action_data over extracted_entities via _val(). This coercion
    populates action_data with the same values as a typed
    CreateLoopExtraction dump so both paths see a consistent shape.

    We keep writing extracted_entities for one release as a compat shim
    — per rfcs/rfc-infer-create-loop-fields.md §Rollout.
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
    ):
        self._llm = llm
        self._langfuse = langfuse
        self._suggestions = suggestion_service
        self._loops = loop_service
        self._draft_service = draft_service
        # Default to empty when not injected — preserves test compatibility
        # and makes the blacklist a strict opt-in for production wiring.
        self._sender_blacklist = sender_blacklist or SenderBlacklist.empty()

    async def on_email(self, event: EmailEvent) -> None:
        """Process an email event — classify and persist suggestions."""
        msg = event.message

        # Outgoing emails on unlinked threads: skip entirely
        if event.direction == MessageDirection.OUTGOING:
            linked_loop = await self._loops.find_loop_by_thread(msg.thread_id)
            if linked_loop is None:
                logger.debug(
                    "skipping outgoing email on unlinked thread %s",
                    msg.thread_id,
                )
                return
            await self._classify_and_persist(event, linked_loop)
            return

        # Incoming email — check for linked loop, classify either way
        linked_loop = await self._loops.find_loop_by_thread(msg.thread_id)

        # Sender blacklist: skip incoming emails from known non-client senders
        # (newsletters, transactional notifications, cold outreach) when the
        # thread isn't already linked to a scheduling loop. We deliberately
        # do NOT apply the blacklist on linked threads — if a newsletter
        # somehow lands inside an active candidate conversation, we still
        # want the classifier to see it.
        if linked_loop is None and self._sender_blacklist.is_blocked(msg.from_.email):
            logger.debug(
                "skipping blacklisted sender %s on unlinked thread %s",
                msg.from_.email,
                msg.thread_id,
            )
            return

        await self._classify_and_persist(event, linked_loop)

    async def _classify_and_persist(
        self,
        event: EmailEvent,
        linked_loop: Loop | None,
    ) -> None:
        """Run the classification pipeline: context → LLM → guardrails → persist."""
        msg = event.message

        # 1. Assemble context
        context_input = await self._build_context(event, linked_loop, event.thread_messages)

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
                loop_id=linked_loop.id if linked_loop else None,
            )
            return

        # 3. Apply guardrails and persist each suggestion
        for item in result.suggestions:
            # Guardrail: drop suggestions that require a loop when thread is unlinked.
            # DRAFT_EMAIL and ADVANCE_STAGE make no sense without a loop — they'll
            # be generated on reclassification after the user creates the loop.
            if linked_loop is None and item.action in (
                SuggestedAction.DRAFT_EMAIL,
                SuggestedAction.ADVANCE_STAGE,
                SuggestedAction.MARK_COLD,
            ):
                logger.info(
                    "dropping %s suggestion — no linked loop (thread %s)",
                    item.action,
                    msg.thread_id,
                )
                continue

            item = self._apply_guardrails(item, linked_loop)
            item = _coerce_create_loop_action_data(item)
            suggestion = await self._suggestions.create_suggestion(
                coordinator_email=event.coordinator_email,
                gmail_message_id=msg.id,
                gmail_thread_id=msg.thread_id,
                item=item,
                reasoning=result.reasoning,
                loop_id=linked_loop.id if linked_loop else None,
                stage_id=self._resolve_stage_id(item, linked_loop),
            )

            logger.info(
                "suggestion created: %s (classification=%s, action=%s, confidence=%.2f)",
                suggestion.id,
                item.classification,
                item.action,
                item.confidence,
            )

            # Draft generation for DRAFT_EMAIL actions
            if (
                item.action == SuggestedAction.DRAFT_EMAIL
                and self._draft_service is not None
                and linked_loop is not None
            ):
                try:
                    await self._draft_service.generate_draft(
                        suggestion=suggestion,
                        loop=linked_loop,
                        thread_messages=event.thread_messages,
                    )
                    logger.info("draft generated for suggestion %s", suggestion.id)
                except Exception:
                    logger.exception("draft generation failed for suggestion %s", suggestion.id)

    async def _build_context(
        self,
        event: EmailEvent,
        linked_loop: Loop | None,
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
        if linked_loop is None:
            coord = await self._loops.get_coordinator_by_email(event.coordinator_email)
            if coord:
                active_loops = await self._get_active_loops(coord.id)

        # Load events for linked loop
        events = []
        if linked_loop:
            events = await self._loops.get_events(linked_loop.id)

        return ClassifyEmailInput(
            stage_states=format_stage_states(),
            transitions=format_transitions(),
            email=format_email(msg, event.direction.value, event.message_type.value),
            thread_history=thread_history_text,
            loop_state=format_loop_state(linked_loop),
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
        linked_loop: Loop | None,
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
        if item.action == SuggestedAction.ADVANCE_STAGE and item.target_state and linked_loop:
            current_stage = self._find_target_stage(item, linked_loop)
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
        linked_loop: Loop | None,
    ) -> str | None:
        """Resolve the stage_id for a suggestion."""
        if item.target_stage_id:
            return item.target_stage_id
        if linked_loop and item.action == SuggestedAction.ADVANCE_STAGE:
            stage = self._find_target_stage(item, linked_loop)
            return stage.id if stage else None
        return None
