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

import sentry_sdk
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
    LinkThreadData,
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

        coord = await self._loops.get_coordinator_by_email(event.coordinator_email)
        active_loops: list[Loop] = []
        if coord:
            active_loops = await self._get_active_loops(coord.id)

        active_loops_count = len(active_loops)
        sentry_sdk.set_measurement("classifier.active_loops_count", active_loops_count)
        scope = sentry_sdk.Scope.get_current_scope()
        scope.set_context(
            "classifier",
            {
                "active_loops_count": active_loops_count,
                "coordinator_email": event.coordinator_email,
            },
        )
        if active_loops_count > 100:
            logger.warning(
                "high active loop count for coordinator %s: %d loops",
                event.coordinator_email,
                active_loops_count,
            )

        email_text = format_email(msg, "incoming", event.message_type.value)
        context_input = self._build_context(
            event,
            coord,
            active_loops,
            email_text,
            event.thread_messages,
        )

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
            item, error = self._apply_guardrails(item, active_loops, email_text)
            if error:
                guardrail_errors.append(error)
            else:
                valid_items.append(item)

        if guardrail_errors and not valid_items:
            error_msg = "; ".join(guardrail_errors)
            logger.warning(
                "all %d suggestions failed guardrails for thread %s, retrying: %s",
                len(result.suggestions),
                msg.thread_id,
                error_msg,
            )
            sentry_sdk.set_tag("classifier.guardrail_retry", "true")
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
                item, error = self._apply_guardrails(item, active_loops, email_text)
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

    def _build_context(
        self,
        event: EmailEvent,
        coord: Coordinator | None,
        active_loops: list[Loop],
        email_text: str,
        thread_messages: list[Message] | None = None,
    ) -> LoopClassifierInput:
        msg = event.message

        if thread_messages:
            thread_history_text = format_thread_history(thread_messages, msg.id)
        else:
            thread_history_text = "No prior messages in this thread."

        coordinator_name = _resolve_coordinator_name(event, coord)
        coordinator_str = f"{coordinator_name}<{event.coordinator_email}>"
        date_str = datetime.now(UTC).date().isoformat()

        return LoopClassifierInput(
            coordinator=coordinator_str,
            date=date_str,
            email=email_text,
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
        active_loops: list[Loop] | None = None,
        email_text: str = "",
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

        # 4. LINK_THREAD semantic validation
        if item.action == SuggestedAction.LINK_THREAD and active_loops and item.target_loop_id:
            error = self._validate_link_thread(item, active_loops, email_text)
            if error:
                return item.model_copy(update={"action": SuggestedAction.NO_ACTION}), error

        # 5. LINK_THREAD confidence floor
        if (
            item.action == SuggestedAction.LINK_THREAD
            and item.confidence < LINK_THREAD_MIN_CONFIDENCE
        ):
            logger.warning(
                "LINK_THREAD confidence %.2f below threshold %.2f for target %s",
                item.confidence,
                LINK_THREAD_MIN_CONFIDENCE,
                item.target_loop_id,
            )
            sentry_sdk.set_tag("classifier.link_thread_demoted", "true")
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

        # 6. CREATE_LOOP must have a real candidate name.
        # The error string feeds into classify()'s error-driven retry: when
        # all suggestions fail guardrails, the errors are joined and injected
        # into the LLM prompt via LoopClassifierInput.error for a second attempt.
        if item.action == SuggestedAction.CREATE_LOOP:
            cand_name = (item.action_data.get("candidate_name") or "").strip()
            if not cand_name or cand_name.lower() == "unknown candidate":
                return (
                    item.model_copy(update={"action": SuggestedAction.NO_ACTION}),
                    "CREATE_LOOP candidate_name must be a real name, not 'Unknown Candidate'. "
                    "Re-read the email to extract the candidate's actual name.",
                )

        return item, None

    @staticmethod
    def _validate_link_thread(
        item: SuggestionItem,
        active_loops: list[Loop],
        email_text: str,
    ) -> str | None:
        """Validate a LINK_THREAD suggestion against active loops and email content.

        Returns an error string for the retry mechanism, or None if valid.
        """
        target = next((lp for lp in active_loops if lp.id == item.target_loop_id), None)

        # Check 1: target loop must exist in active loops
        if target is None:
            return (
                f"LINK_THREAD target_loop_id '{item.target_loop_id}' not found in "
                f"active loops — the loop may have been completed or archived"
            )

        # Check 2: LLM extraction must match target loop
        try:
            extraction = LinkThreadData.model_validate(item.action_data)
        except ValidationError:
            extraction = None

        if (
            extraction
            and target.candidate
            and extraction.candidate_name.lower().strip() != target.candidate.name.lower().strip()
        ):
            return (
                f"LINK_THREAD action_data says candidate is '{extraction.candidate_name}' "
                f"but target loop '{target.id}' is for candidate "
                f"'{target.candidate.name}' — these don't match. "
                f"Use CREATE_LOOP if this is a different candidate."
            )

        target_company = (
            target.client_contact.company
            if target.client_contact and target.client_contact.company
            else None
        )
        if (
            extraction
            and target_company
            and extraction.client_company.lower().strip() != target_company.lower().strip()
        ):
            return (
                f"LINK_THREAD action_data says client is '{extraction.client_company}' "
                f"but target loop '{target.id}' is for client "
                f"'{target_company}' — these don't match. "
                f"Use CREATE_LOOP if this is a different client."
            )

        # Check 3: target loop's candidate last name must appear in email
        if target.candidate and email_text:
            candidate_name = target.candidate.name.strip()
            parts = candidate_name.split()
            last_name = parts[-1] if parts else ""
            if last_name and len(last_name) > 1:
                email_lower = email_text.lower()
                if last_name.lower() not in email_lower:
                    return (
                        f"LINK_THREAD target loop is for candidate '{candidate_name}' "
                        f"but the name '{last_name}' does not appear in the email — "
                        f"use CREATE_LOOP if this is a new candidate."
                    )

        return None
