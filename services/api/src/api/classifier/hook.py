"""ClassifierHook — the email classification agent.

Implements the EmailHook protocol to classify incoming emails,
match them to scheduling loops, and persist structured suggestions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langfuse import observe

from api.ai.endpoint import llm_endpoint
from api.ai.errors import AIError
from api.classifier.models import (
    ClassificationResult,
    EmailClassification,
    SuggestedAction,
    SuggestionItem,
)
from api.classifier.prompts import (
    format_classification_schema,
    format_email,
    format_events,
    format_loop_state,
    format_stage_states,
    format_thread_history,
    format_transitions,
)
from api.gmail.hooks import EmailEvent, MessageDirection
from api.scheduling.models import ALLOWED_TRANSITIONS

if TYPE_CHECKING:
    from langfuse import Langfuse
    from psycopg_pool import AsyncConnectionPool

    from api.ai.llm_service import LLMService
    from api.classifier.suggestions import SuggestionService
    from api.scheduling.service import LoopService

logger = logging.getLogger(__name__)

# The typed endpoint for email classification
classify_email = llm_endpoint(
    name="classify_email",
    system_prompt_name="scheduling-classifier-v2",
    user_prompt_name="scheduling-classifier-user-v2",
    output_type=ClassificationResult,
)

# Confidence threshold for LINK_THREAD suggestions
LINK_THREAD_MIN_CONFIDENCE = 0.9

# Maximum classifications per coordinator per hour (rate limit)
MAX_CLASSIFICATIONS_PER_HOUR = 100


class ClassifierHook:
    """Email classification agent implementing the EmailHook protocol.

    Replaces LoggingHook in the push pipeline. For each email:
    1. Assembles context (thread, loop state, active loops)
    2. Calls the LLM classifier via typed endpoint
    3. Validates and adjusts suggestions (guardrails)
    4. Persists suggestions to the database
    """

    def __init__(
        self,
        *,
        llm: LLMService,
        langfuse: Langfuse,
        loop_service: LoopService,
        suggestion_service: SuggestionService,
        db_pool: AsyncConnectionPool,
    ):
        self._llm = llm
        self._langfuse = langfuse
        self._loop_service = loop_service
        self._suggestions = suggestion_service
        self._pool = db_pool

    @observe(name="classifier_hook")
    async def on_email(self, event: EmailEvent) -> None:
        """Process an email event through the classification pipeline."""
        msg = event.message
        logger.info(
            "Classifying email: thread=%s message=%s direction=%s",
            msg.thread_id,
            msg.id,
            event.direction.value,
        )

        # Look up if this thread is linked to a loop
        loop = await self._loop_service.find_loop_by_thread(msg.thread_id)

        # Outgoing emails: only classify if thread is linked to a loop
        if event.direction == MessageDirection.OUTGOING:
            if loop is None:
                logger.debug("Skipping outgoing email on unlinked thread %s", msg.thread_id)
                return
            await self._classify_outgoing(event, loop)
            return

        # Incoming emails: full classification pipeline
        await self._classify_incoming(event, loop)

    async def _classify_incoming(self, event: EmailEvent, loop) -> None:
        """Classify an incoming email and persist suggestions."""
        msg = event.message

        try:
            # Assemble context
            variables = await self._build_context(event, loop)

            # Call LLM classifier
            result = await classify_email(
                llm=self._llm,
                langfuse=self._langfuse,
                variables=variables,
            )

            logger.info(
                "Classification result: %d suggestions, classifications=%s",
                len(result.suggestions),
                [s.classification.value for s in result.suggestions],
            )

            # Apply guardrails and persist each suggestion
            for item in result.suggestions:
                item = self._apply_guardrails(item, loop)
                await self._suggestions.create_suggestion(
                    coordinator_email=event.coordinator_email,
                    gmail_message_id=msg.id,
                    gmail_thread_id=msg.thread_id,
                    item=item,
                    reasoning=result.reasoning,
                    loop_id=loop.id if loop else None,
                    stage_id=item.target_stage_id,
                )

            logger.info(
                "Persisted %d suggestions for thread=%s",
                len(result.suggestions),
                msg.thread_id,
            )

        except AIError as exc:
            logger.error("Classification failed for message %s: %s", msg.id, exc)
            # Create a NEEDS_ATTENTION fallback suggestion
            fallback = SuggestionItem(
                classification=EmailClassification.FOLLOW_UP_NEEDED,
                action=SuggestedAction.ASK_COORDINATOR,
                confidence=0.0,
                summary=(
                    f"Classification failed: {type(exc).__name__}. "
                    "Please review this email manually."
                ),
                questions=["What action should be taken for this email?"],
            )
            await self._suggestions.create_suggestion(
                coordinator_email=event.coordinator_email,
                gmail_message_id=msg.id,
                gmail_thread_id=msg.thread_id,
                item=fallback,
                reasoning=f"Classification error: {exc}",
                loop_id=loop.id if loop else None,
            )

    async def _classify_outgoing(self, event: EmailEvent, loop) -> None:
        """Classify an outgoing email for state sync."""
        msg = event.message

        try:
            variables = await self._build_context(event, loop)

            result = await classify_email(
                llm=self._llm,
                langfuse=self._langfuse,
                variables=variables,
            )

            for item in result.suggestions:
                # Outgoing emails should have auto_advance=true
                item.auto_advance = True
                item = self._apply_guardrails(item, loop)

                # Skip no-ops for outgoing emails
                if item.action == SuggestedAction.NO_ACTION:
                    continue

                if item.action == SuggestedAction.ADVANCE_STAGE and item.target_state:
                    # Supersede stale pending suggestions BEFORE advancing
                    await self._suggestions.supersede_pending_for_loop(
                        loop_id=loop.id,
                        resolved_by=event.coordinator_email,
                    )

                    # Auto-advance the stage
                    stage = loop.most_urgent_stage
                    if stage and item.target_state in ALLOWED_TRANSITIONS.get(stage.state, set()):
                        await self._loop_service.advance_stage(
                            stage_id=stage.id,
                            to_state=item.target_state,
                            coordinator_email=event.coordinator_email,
                            triggered_by="classifier_auto_advance",
                        )
                        logger.info(
                            "Auto-advanced stage %s: %s → %s",
                            stage.id,
                            stage.state.value,
                            item.target_state.value,
                        )

                # Persist the auto-applied suggestion
                await self._suggestions.create_suggestion(
                    coordinator_email=event.coordinator_email,
                    gmail_message_id=msg.id,
                    gmail_thread_id=msg.thread_id,
                    item=item,
                    reasoning=result.reasoning,
                    loop_id=loop.id,
                    stage_id=item.target_stage_id,
                )

        except AIError as exc:
            # Outgoing classification failures are non-critical — log and move on
            logger.warning("Outgoing classification failed for %s: %s", msg.id, exc)

    async def _build_context(self, event: EmailEvent, loop) -> dict[str, str]:
        """Assemble all template variables for the classifier prompt."""
        msg = event.message

        # Format the current email
        email_text = format_email(msg)

        # Get thread messages for history (the worker attaches these via _thread_messages)
        thread_messages = getattr(event, "_thread_messages", None)
        thread_history = (
            format_thread_history(thread_messages, exclude_id=msg.id)
            if thread_messages
            else "(thread context unavailable)"
        )

        # Loop state
        loop_state_text = format_loop_state(loop)

        # Events for the loop
        events_text = "(no events)"
        if loop:
            events = await self._loop_service.get_events(loop.id)
            events_text = format_events(events)

        # Active loops summary (for thread matching on unlinked threads)
        active_loops_text = "(no active loops)"
        if loop is None:
            board = await self._loop_service.get_status_board(event.coordinator_email)
            all_summaries = board.action_needed + board.waiting + board.scheduled
            if all_summaries:
                # We need full Loop objects for format_active_loops
                # Use summaries directly for a lightweight version
                lines = []
                for s in all_summaries:
                    lines.append(
                        f"  - {s.title}: {s.candidate_name} at {s.client_company} "
                        f"[{s.most_urgent_state.value if s.most_urgent_state else 'unknown'}] "
                        f"(ID: {s.loop_id})"
                    )
                active_loops_text = "\n".join(lines) if lines else "(no active loops)"

        return {
            "email": email_text,
            "thread_history": thread_history,
            "loop_state": loop_state_text,
            "active_loops_summary": active_loops_text,
            "events": events_text,
            "direction": event.direction.value,
            "stage_states": format_stage_states(),
            "transitions": format_transitions(),
            "classification_schema": format_classification_schema(),
        }

    def _apply_guardrails(self, item: SuggestionItem, loop) -> SuggestionItem:
        """Post-LLM guardrails: validate and adjust suggestions."""

        # Guardrail 1: LINK_THREAD requires high confidence
        if (
            item.action == SuggestedAction.LINK_THREAD
            and item.confidence < LINK_THREAD_MIN_CONFIDENCE
        ):
            logger.info(
                "Demoting LINK_THREAD to CREATE_LOOP (confidence %.2f < %.2f)",
                item.confidence,
                LINK_THREAD_MIN_CONFIDENCE,
            )
            item = item.model_copy(
                update={
                    "action": SuggestedAction.CREATE_LOOP,
                    "summary": f"{item.summary} (confidence too low to link, suggesting new loop)",
                }
            )

        # Guardrail 2: Validate state transitions
        if item.action == SuggestedAction.ADVANCE_STAGE and item.target_state and loop:
            stage = loop.most_urgent_stage
            if stage:
                allowed = ALLOWED_TRANSITIONS.get(stage.state, set())
                if item.target_state not in allowed:
                    logger.warning(
                        "Invalid transition %s → %s, demoting to ASK_COORDINATOR",
                        stage.state.value,
                        item.target_state.value,
                    )
                    item = item.model_copy(
                        update={
                            "action": SuggestedAction.ASK_COORDINATOR,
                            "summary": (
                                f"{item.summary} (suggested transition "
                                f"{stage.state.value} → {item.target_state.value} "
                                f"is not valid)"
                            ),
                            "questions": [
                                f"The classifier suggested advancing from "
                                f"{stage.state.value} to {item.target_state.value}, "
                                f"but this transition isn't allowed. What should happen?"
                            ],
                        }
                    )

        return item
