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
    format_thread_history,
)
from api.classifier.models import (
    ClassificationResult,
    SuggestedAction,
    SuggestionItem,
)
from api.classifier.service import SuggestionService  # noqa: TC001 — used at runtime in __init__
from api.gmail.hooks import EmailEvent, MessageDirection
from api.scheduling.models import ALLOWED_TRANSITIONS, StageState

if TYPE_CHECKING:
    from langfuse import Langfuse

    from api.ai.llm_service import LLMService
    from api.gmail.models import Message
    from api.scheduling.models import Loop, Stage
    from api.scheduling.service import LoopService

logger = logging.getLogger(__name__)

# Guardrail: LINK_THREAD requires >= this confidence
LINK_THREAD_MIN_CONFIDENCE = 0.9

# Rate limit: max classifications per coordinator per hour
MAX_CLASSIFICATIONS_PER_HOUR = 100


class ClassifierHook:
    """Email classification agent — implements the EmailHook protocol."""

    def __init__(
        self,
        *,
        llm: LLMService,
        langfuse: Langfuse,
        suggestion_service: SuggestionService,
        loop_service: LoopService,
    ):
        self._llm = llm
        self._langfuse = langfuse
        self._suggestions = suggestion_service
        self._loops = loop_service

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
            item = self._apply_guardrails(item, linked_loop)
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

        # 4. Outgoing email state sync: auto-advance and supersede
        if event.direction == MessageDirection.OUTGOING and linked_loop:
            await self._handle_outgoing_state_sync(result, linked_loop, event.coordinator_email)

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

    async def _handle_outgoing_state_sync(
        self,
        result: ClassificationResult,
        loop: Loop,
        coordinator_email: str,
    ) -> None:
        """For outgoing emails: auto-advance stages and supersede stale suggestions."""
        for item in result.suggestions:
            if not item.auto_advance or item.action != SuggestedAction.ADVANCE_STAGE:
                continue

            if not item.target_state:
                continue

            stage = self._find_target_stage(item, loop)
            if not stage:
                continue

            target = StageState(item.target_state)
            allowed = ALLOWED_TRANSITIONS.get(stage.state, set())
            if target not in allowed:
                logger.warning(
                    "outgoing state sync: invalid transition %s → %s, skipping",
                    stage.state,
                    target,
                )
                continue

            try:
                await self._loops.advance_stage(
                    stage_id=stage.id,
                    to_state=target,
                    coordinator_email=coordinator_email,
                    triggered_by="classifier_outgoing_sync",
                )
                logger.info(
                    "auto-advanced stage %s: %s → %s",
                    stage.id,
                    stage.state,
                    target,
                )
            except Exception:
                logger.exception("failed to auto-advance stage %s", stage.id)
                continue

            # Supersede stale pending suggestions for this loop
            await self._suggestions.supersede_pending_for_loop(
                loop_id=loop.id,
                resolved_by=f"auto:{coordinator_email}",
            )
