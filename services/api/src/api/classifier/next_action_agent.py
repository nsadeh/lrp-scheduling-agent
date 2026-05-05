"""NextActionAgent — handles emails on threads already linked to a loop.

Processes both inbound and outgoing emails. Decides on next steps:
- Advance the loop's state (ADVANCE_STAGE — auto-resolved)
- Draft an email (DRAFT_EMAIL — generates draft for coordinator review)
- Ask the coordinator a question (ASK_COORDINATOR)
- No action (NO_ACTION)

CREATE_LOOP and LINK_THREAD are blacklisted to prevent recursion.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError

from api.classifier.endpoints import determine_next_action
from api.classifier.formatters import (
    format_email,
    format_events,
    format_linked_loops,
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
    build_agent_registry,
    try_auto_resolve,
)
from api.classifier.schemas import NextActionInput

if TYPE_CHECKING:
    from arq.connections import ArqRedis
    from langfuse import Langfuse

    from api.ai.llm_service import LLMService
    from api.classifier.service import SuggestionService
    from api.drafts.service import DraftService
    from api.gmail.hooks import EmailEvent
    from api.gmail.models import Message
    from api.scheduling.models import Coordinator, Loop
    from api.scheduling.service import LoopService

logger = logging.getLogger(__name__)

_AGENT_ALLOWED_ACTIONS = frozenset(
    {
        SuggestedAction.ADVANCE_STAGE,
        SuggestedAction.DRAFT_EMAIL,
        SuggestedAction.ASK_COORDINATOR,
        SuggestedAction.NO_ACTION,
    }
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


class NextActionAgent:
    """Determines next steps for emails on threads linked to a loop."""

    def __init__(
        self,
        *,
        llm: LLMService,
        langfuse: Langfuse,
        suggestion_service: SuggestionService,
        loop_service: LoopService,
        draft_service: DraftService | None = None,
    ):
        self._llm = llm
        self._langfuse = langfuse
        self._suggestions = suggestion_service
        self._loops = loop_service
        self._draft_service = draft_service
        self._resolver_registry = build_agent_registry()

    async def act(
        self,
        event: EmailEvent,
        linked_loops: list[Loop],
        *,
        arq_pool: ArqRedis | None = None,
    ) -> None:
        msg = event.message

        context_input = await self._build_context(event, linked_loops, event.thread_messages)

        try:
            result: ClassificationResult = await determine_next_action(
                llm=self._llm,
                langfuse=self._langfuse,
                data=context_input,
            )
        except Exception:
            logger.exception(
                "next action agent failed for message %s on thread %s",
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
                    summary="Action determination failed — please review this email manually.",
                    reasoning="LLM call failed",
                    target_loop_id=linked_loops[0].id if linked_loops else None,
                    action_data={
                        "question": (
                            "The next action agent encountered an error processing this email."
                        )
                    },
                ),
                reasoning="LLM call failed",
                loop_id=linked_loops[0].id if linked_loops else None,
            )
            return

        # Apply guardrails with error-driven retry
        guardrail_errors: list[str] = []
        valid_items: list[tuple[SuggestionItem, Loop | None]] = []

        for item in result.suggestions:
            target_loop, loop_error = self._resolve_target_loop(item, linked_loops)

            if loop_error:
                guardrail_errors.append(loop_error)
                continue

            # If we defaulted to the single linked loop, pin its id on the item
            # so guardrails (and persistence) see a populated target_loop_id.
            if target_loop and not item.target_loop_id:
                item = item.model_copy(update={"target_loop_id": target_loop.id})

            item, error = self._apply_guardrails(item)
            if error:
                guardrail_errors.append(error)
            else:
                valid_items.append((item, target_loop))

        if guardrail_errors and not valid_items:
            error_msg = "; ".join(guardrail_errors)
            logger.info(
                "all agent suggestions failed guardrails, retrying with error: %s",
                error_msg,
            )
            retry_input = context_input.model_copy(update={"error": error_msg})
            try:
                result = await determine_next_action(
                    llm=self._llm,
                    langfuse=self._langfuse,
                    data=retry_input,
                )
            except Exception:
                logger.exception("next action agent retry failed for thread %s", msg.thread_id)
                return

            valid_items = []
            for item in result.suggestions:
                target_loop, loop_error = self._resolve_target_loop(item, linked_loops)
                if loop_error:
                    continue
                if target_loop and not item.target_loop_id:
                    item = item.model_copy(update={"target_loop_id": target_loop.id})
                item, error = self._apply_guardrails(item)
                if not error:
                    valid_items.append((item, target_loop))

        for item, target_loop in valid_items:
            suggestion = await self._suggestions.create_suggestion(
                coordinator_email=event.coordinator_email,
                gmail_message_id=msg.id,
                gmail_thread_id=msg.thread_id,
                item=item,
                reasoning=result.reasoning,
                loop_id=target_loop.id if target_loop else None,
            )

            logger.info(
                "agent suggestion created: %s (action=%s, confidence=%.2f)",
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
            applied = await try_auto_resolve(suggestion, ctx, self._resolver_registry)
            if applied:
                continue

            if (
                item.action == SuggestedAction.DRAFT_EMAIL
                and self._draft_service is not None
                and target_loop is not None
            ):
                try:
                    draft_body = item.action_data.get("body", "")
                    await self._draft_service.generate_draft(
                        suggestion=suggestion,
                        loop=target_loop,
                        thread_messages=event.thread_messages,
                        body=draft_body,
                    )
                    logger.info("draft created for suggestion %s", suggestion.id)
                except Exception:
                    logger.exception("draft creation failed for suggestion %s", suggestion.id)

    async def _build_context(
        self,
        event: EmailEvent,
        linked_loops: list[Loop],
        thread_messages: list[Message] | None = None,
    ) -> NextActionInput:
        msg = event.message

        if thread_messages:
            thread_history_text = format_thread_history(thread_messages, msg.id)
        else:
            thread_history_text = "No prior messages in this thread."

        coord = await self._loops.get_coordinator_by_email(event.coordinator_email)
        coordinator_name = _resolve_coordinator_name(event, coord)
        coordinator_str = f"{coordinator_name}<{event.coordinator_email}>"
        date_str = datetime.now(UTC).date().isoformat()

        events = []
        if linked_loops:
            events = await self._loops.get_events(linked_loops[0].id)

        primary = linked_loops[0] if linked_loops else None
        candidate_name = primary.candidate.name if primary and primary.candidate else "Unknown"
        recruiter_name = primary.recruiter.name if primary and primary.recruiter else "Unknown"
        client_name = (
            primary.client_contact.name if primary and primary.client_contact else "Unknown"
        )
        client_company = (
            primary.client_contact.company
            if primary and primary.client_contact and primary.client_contact.company
            else "Unknown"
        )

        return NextActionInput(
            coordinator=coordinator_str,
            date=date_str,
            candidate_name=candidate_name,
            recruiter_name=recruiter_name,
            client_name=client_name,
            client_company=client_company,
            direction=event.direction.value,
            email=format_email(msg, event.direction.value, event.message_type.value),
            thread_history=thread_history_text,
            loop_state=format_linked_loops(linked_loops),
            events=format_events(events),
            error="N/A",
        )

    def _resolve_target_loop(
        self,
        item: SuggestionItem,
        linked_loops: list[Loop],
    ) -> tuple[Loop | None, str | None]:
        """Resolve the target loop for a suggestion. Returns (loop, error).

        The agent only operates on linked threads, so target_loop_id is required
        for every action. The guardrail layer enforces that — this just maps the
        ID to a Loop instance.
        """
        loop_ids = [lp.id for lp in linked_loops]

        if item.target_loop_id:
            for loop in linked_loops:
                if loop.id == item.target_loop_id:
                    return loop, None
            return None, (
                f"target_loop_id '{item.target_loop_id}' does not match any linked loop. "
                f"Available loop IDs: {', '.join(loop_ids)}"
            )

        # No target_loop_id — fine only if exactly one loop is linked AND we
        # default to that loop. The required-target_loop_id guardrail then
        # tags the suggestion with the resolved id at persistence time.
        if len(linked_loops) == 1:
            return linked_loops[0], None

        return None, (
            f"target_loop_id is required (the agent only operates on linked threads). "
            f"Available loop IDs: {', '.join(loop_ids)}"
        )

    def _apply_guardrails(
        self,
        item: SuggestionItem,
    ) -> tuple[SuggestionItem, str | None]:
        """Apply guardrails. Returns (item, error_message). error_message is None if valid."""
        # 1. Action allow-list
        if item.action not in _AGENT_ALLOWED_ACTIONS:
            return (
                item.model_copy(update={"action": SuggestedAction.NO_ACTION}),
                f"Action '{item.action}' is not allowed for the next action agent — "
                f"only advance_stage, draft_email, ask_coordinator, and no_action are allowed",
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

        # 3. target_loop_id required for all agent actions (the agent only
        #    operates on linked threads, so every suggestion is *about* a loop)
        if not item.target_loop_id:
            return (
                item.model_copy(update={"action": SuggestedAction.NO_ACTION}),
                f"action '{item.action}' requires target_loop_id "
                f"(the agent always acts on a linked loop)",
            )

        return item, None
