"""LoopClassifier — handles emails on threads not yet linked to any loop.

Only processes inbound emails. Decides whether to:
- Create a new scheduling loop (CREATE_LOOP)
- Attach the thread to an existing loop (LINK_THREAD)
- Take no action (NO_ACTION)

After a CREATE_LOOP or LINK_THREAD auto-resolves, enqueues the
NextActionAgent to determine next steps on the now-linked thread.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError

from api.classifier.endpoints import classify_new_thread
from api.classifier.formatters import (
    format_active_loops,
    format_email,
    format_thread_history,
)
from api.classifier.models import (
    ACTION_DATA_MODELS,
    ClassificationResult,
    SuggestedAction,
    SuggestionItem,
)
from api.classifier.resolvers import (
    ResolverContext,
    build_classifier_registry,
    try_auto_resolve,
)
from api.classifier.schemas import LoopClassifierInput

if TYPE_CHECKING:
    from arq.connections import ArqRedis
    from langfuse import Langfuse

    from api.ai.llm_service import LLMService
    from api.classifier.service import SuggestionService
    from api.gmail.hooks import EmailEvent
    from api.gmail.models import Message
    from api.scheduling.models import Coordinator, Loop
    from api.scheduling.service import LoopService

logger = logging.getLogger(__name__)

LINK_THREAD_MIN_CONFIDENCE = 0.9

_CLASSIFIER_ALLOWED_ACTIONS = frozenset(
    {SuggestedAction.CREATE_LOOP, SuggestedAction.LINK_THREAD, SuggestedAction.NO_ACTION}
)


def _resolve_coordinator_name(event: EmailEvent, coord: Coordinator | None) -> str:
    if coord and coord.name:
        return coord.name

    msg = event.message
    addr_email = event.coordinator_email
    candidates: list[str | None] = []
    if msg.from_.email == addr_email:
        candidates.append(msg.from_.name)
    for addr in [*msg.to, *msg.cc]:
        if addr.email == addr_email:
            candidates.append(addr.name)
    for name in candidates:
        if name:
            return name

    return addr_email.split("@", 1)[0]


class LoopClassifier:
    """Classifies unlinked inbound threads — create loop, link, or ignore."""

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
        self._resolver_registry = build_classifier_registry()

    async def classify(
        self,
        event: EmailEvent,
        *,
        arq_pool: ArqRedis | None = None,
    ) -> None:
        msg = event.message

        context_input = await self._build_context(event, event.thread_messages)

        # First attempt
        try:
            result: ClassificationResult = await classify_new_thread(
                llm=self._llm,
                langfuse=self._langfuse,
                data=context_input,
            )
        except Exception:
            logger.exception(
                "loop classifier failed for message %s on thread %s",
                msg.id,
                msg.thread_id,
            )
            await self._suggestions.create_suggestion(
                coordinator_email=event.coordinator_email,
                gmail_message_id=msg.id,
                gmail_thread_id=msg.thread_id,
                item=SuggestionItem(
                    classification="follow_up_needed",
                    action="ask_coordinator",
                    confidence=0.0,
                    summary="Classification failed — please review this email manually.",
                    reasoning="LLM call failed",
                    action_data={
                        "question": (
                            "The loop classifier encountered an error processing this email."
                        )
                    },
                ),
                reasoning="LLM call failed",
            )
            return

        # Apply guardrails with error-driven retry
        guardrail_errors: list[str] = []
        valid_items: list[SuggestionItem] = []

        for item in result.suggestions:
            item, error = self._apply_guardrails(item)
            if error:
                guardrail_errors.append(error)
            else:
                valid_items.append(item)

        if guardrail_errors and not valid_items:
            # All suggestions failed guardrails — retry with error feedback
            error_msg = "; ".join(guardrail_errors)
            logger.info(
                "all suggestions failed guardrails, retrying with error: %s",
                error_msg,
            )
            retry_input = context_input.model_copy(update={"error": error_msg})
            try:
                result = await classify_new_thread(
                    llm=self._llm,
                    langfuse=self._langfuse,
                    data=retry_input,
                )
            except Exception:
                logger.exception("loop classifier retry failed for thread %s", msg.thread_id)
                return

            valid_items = []
            for item in result.suggestions:
                item, error = self._apply_guardrails(item)
                if not error:
                    valid_items.append(item)

        for item in valid_items:
            suggestion = await self._suggestions.create_suggestion(
                coordinator_email=event.coordinator_email,
                gmail_message_id=msg.id,
                gmail_thread_id=msg.thread_id,
                item=item,
                reasoning=result.reasoning,
            )

            logger.info(
                "classifier suggestion created: %s (action=%s, confidence=%.2f)",
                suggestion.id,
                item.action,
                item.confidence,
            )

            ctx = ResolverContext(
                coordinator_email=event.coordinator_email,
                gmail_thread_id=msg.thread_id,
                gmail_message_id=msg.id,
                gmail_subject=msg.subject,
                loop_service=self._loops,
                suggestion_service=self._suggestions,
                arq_pool=arq_pool,
            )
            await try_auto_resolve(suggestion, ctx, self._resolver_registry)

    async def _build_context(
        self,
        event: EmailEvent,
        thread_messages: list[Message] | None = None,
    ) -> LoopClassifierInput:
        msg = event.message

        if thread_messages:
            thread_history_text = format_thread_history(thread_messages, msg.id)
        else:
            thread_history_text = "No prior messages in this thread."

        coord = await self._loops.get_coordinator_by_email(event.coordinator_email)
        active_loops: list[Loop] = []
        if coord:
            active_loops = await self._get_active_loops(coord.id)

        coordinator_name = _resolve_coordinator_name(event, coord)
        coordinator_str = f"{coordinator_name}<{event.coordinator_email}>"
        date_str = datetime.now(UTC).date().isoformat()

        return LoopClassifierInput(
            coordinator=coordinator_str,
            date=date_str,
            email=format_email(msg, "incoming", event.message_type.value),
            thread_history=thread_history_text,
            active_loops_summary=format_active_loops(active_loops),
            error="N/A",
        )

    async def _get_active_loops(self, coordinator_id: str) -> list[Loop]:
        from api.scheduling.queries import queries as sched_queries
        from api.scheduling.service import _fetch_dicts, _row_to_loop_full

        async with self._loops._pool.connection() as conn:
            rows = await _fetch_dicts(
                conn,
                sched_queries.get_active_loops_full_for_coordinator,
                coordinator_id=coordinator_id,
            )

        loops = [_row_to_loop_full(r) for r in rows]
        return await self._loops._hydrate_loop_relations(loops)

    def _apply_guardrails(
        self,
        item: SuggestionItem,
    ) -> tuple[SuggestionItem, str | None]:
        """Apply guardrails. Returns (item, error_message). error_message is None if valid."""
        # 1. Action allow-list
        if item.action not in _CLASSIFIER_ALLOWED_ACTIONS:
            return (
                item.model_copy(update={"action": SuggestedAction.NO_ACTION}),
                f"Action '{item.action}' is not valid for the loop classifier — "
                f"only create_loop, link_thread, and no_action are allowed",
            )

        # 2. action_data shape match
        model_cls = ACTION_DATA_MODELS.get(item.action)
        if model_cls is None:
            return (
                item.model_copy(update={"action": SuggestedAction.NO_ACTION}),
                f"action '{item.action}' has no action_data schema",
            )
        try:
            model_cls.model_validate(item.action_data)
        except ValidationError as e:
            return (
                item.model_copy(update={"action": SuggestedAction.NO_ACTION}),
                f"action_data for '{item.action}' is invalid: {e}",
            )

        # 3. target_loop_id required for LINK_THREAD
        if item.action == SuggestedAction.LINK_THREAD and not item.target_loop_id:
            return (
                item.model_copy(update={"action": SuggestedAction.NO_ACTION}),
                "LINK_THREAD requires target_loop_id to identify which loop to link to",
            )

        # 4. LINK_THREAD confidence floor
        if (
            item.action == SuggestedAction.LINK_THREAD
            and item.confidence < LINK_THREAD_MIN_CONFIDENCE
        ):
            return (
                item.model_copy(
                    update={
                        "action": SuggestedAction.CREATE_LOOP,
                        "summary": f"{item.summary} (link confidence too low, suggesting new loop)",
                    }
                ),
                f"LINK_THREAD confidence {item.confidence:.2f} is below the "
                f"{LINK_THREAD_MIN_CONFIDENCE} threshold — either increase confidence "
                f"or use CREATE_LOOP",
            )

        return item, None
