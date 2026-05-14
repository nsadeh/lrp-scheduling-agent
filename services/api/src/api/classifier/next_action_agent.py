"""NextActionAgent — handles emails on threads already linked to a loop.

Processes both inbound and outgoing emails. Decides on next steps:
- Advance the loop's state (ADVANCE_STAGE — auto-resolved)
- Draft an email (DRAFT_EMAIL — generates draft for coordinator review)
- Ask the coordinator a question (ASK_COORDINATOR)
- Expire a stale pending suggestion (EXPIRE_SUGGESTION — auto-resolved)
- No action (NO_ACTION)

CREATE_LOOP and LINK_THREAD are blacklisted to prevent recursion.

I/O contract (new prompt):
- Input is four XML-formatted template variables: ``date``, ``thread_history``,
  ``email``, ``loops``.
- Output is a JSON array wrapped in ``<suggestions>…</suggestions>`` tags.
- Retries (errors, coordinator responses) ride the LLM conversation history
  rather than being threaded back through input fields.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import sentry_sdk
from langfuse.model import ChatPromptClient
from pydantic import ValidationError

from api.ai.langfuse_client import fetch_prompt
from api.ai.llm_service import DEFAULT_MODEL
from api.classifier.agent_runtime import (
    build_error_followup,
    diagnostics_from_response,
    parse_suggestions_envelope,
)
from api.classifier.formatters import (
    format_email_xml,
    format_llm_datetime,
    format_loops_xml,
    format_thread_history_xml,
)
from api.classifier.generation import is_current_generation
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

    from api.ai.llm_service import LLMResponse, LLMService
    from api.classifier.models import Suggestion
    from api.classifier.service import SuggestionService
    from api.drafts.service import DraftService
    from api.gmail.hooks import EmailEvent
    from api.gmail.models import Message
    from api.scheduling.models import Loop
    from api.scheduling.service import LoopService

logger = logging.getLogger(__name__)

_AGENT_ALLOWED_ACTIONS = frozenset(
    {
        SuggestedAction.ADVANCE_STAGE,
        SuggestedAction.DRAFT_EMAIL,
        SuggestedAction.ASK_COORDINATOR,
        SuggestedAction.EXPIRE_SUGGESTION,
        SuggestedAction.UPDATE_ACTOR,
        SuggestedAction.NO_ACTION,
    }
)


def _suggestion_fingerprint(loop_id: str | None, action: str, action_data: dict) -> str:
    """Canonical fingerprint for deduplication: loop_id + action + normalized action_data."""
    return f"{loop_id or ''}|{action}|{json.dumps(action_data, sort_keys=True, default=str)}"


class NextActionAgentError(Exception):
    """Raised when the agent cannot produce a usable response.

    Carries ``raw_responses`` and ``call_diagnostics`` so the caller can
    write the unparsed LLM output AND per-call diagnostic metadata
    (finish_reason, completion_tokens, model, provider, latency) to the
    LangFuse span when validation fails. Without ``call_diagnostics`` we
    can't distinguish "natural stop with bad content" from "length
    truncation" from "content filter" — they all surface as the same
    parse error at the agent layer.
    """

    def __init__(
        self,
        message: str,
        *,
        raw_responses: list[str] | None = None,
        call_diagnostics: list[dict] | None = None,
    ):
        super().__init__(message)
        self.raw_responses = list(raw_responses or [])
        self.call_diagnostics = list(call_diagnostics or [])


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
        coordinator_response: str | None = None,
        originating_suggestion: Suggestion | None = None,
        rejected_suggestion: Suggestion | None = None,
        generation_token: str | None = None,
        generation_thread_id: str | None = None,
    ) -> None:
        msg = event.message

        context_input, existing_pending = await self._build_context(
            event,
            linked_loops,
            event.thread_messages,
        )

        try:
            prompt = fetch_prompt(self._langfuse, "next-action-agent")
            with self._langfuse.start_as_current_observation(
                name="next-action-agent",
            ):
                # Span's ``input`` is set inside _call_llm to the live messages
                # list at the moment of each LLM round-trip — that captures
                # retry follow-ups (error feedback, coordinator responses,
                # rejection prompts) that wouldn't be visible if we just
                # serialized the NextActionInput dataclass. The structured
                # context lives in metadata for reference.
                self._langfuse.update_current_span(
                    metadata={
                        "prompt_name": "next-action-agent",
                        "prompt_version": prompt.version,
                        "prompt_labels": prompt.labels,
                        "context": context_input.model_dump(),
                    }
                )
                messages = self._build_initial_messages(
                    prompt,
                    context_input,
                    coordinator_response=coordinator_response,
                    originating_suggestion=originating_suggestion,
                    rejected_suggestion=rejected_suggestion,
                )
                # Checkpoint 1: another worker may have started a newer run on
                # this thread while we were building context. Skip the LLM
                # round-trip entirely if we've been superseded.
                if not await is_current_generation(
                    arq_pool, generation_thread_id, generation_token
                ):
                    logger.info(
                        "next-action-agent superseded before LLM (thread=%s, token=%s)",
                        generation_thread_id,
                        generation_token,
                    )
                    return
                try:
                    valid_items, raw_responses, call_diagnostics = await self._act_with_messages(
                        prompt,
                        messages,
                        linked_loops,
                        existing_pending,
                        allow_retry=True,
                    )
                except NextActionAgentError as exc:
                    # The model produced something — we just couldn't use it
                    # (schema mismatch, malformed JSON, etc.). Put the raw
                    # content AND per-call diagnostics (finish_reason,
                    # completion_tokens, model, provider, latency) on the
                    # LangFuse span before the exception escapes this with-
                    # block. The diagnostics are load-bearing for diagnosing
                    # WHY the response was bad: a finish_reason of "stop"
                    # with completion_tokens far below max_tokens points to
                    # the model emitting an early EOS rather than length
                    # truncation. The outer except creates the manual-review
                    # fallback.
                    self._langfuse.update_current_span(
                        output={
                            "raw_responses": exc.raw_responses,
                            "call_diagnostics": exc.call_diagnostics,
                            "parse_error": str(exc),
                        }
                    )
                    # Sentry context so the existing Sentry error captures
                    # surface the diagnostic fields without us needing to
                    # query LangFuse to correlate.
                    if exc.call_diagnostics:
                        sentry_sdk.set_context(
                            "next_action_agent",
                            {
                                "call_count": len(exc.call_diagnostics),
                                "last_call": exc.call_diagnostics[-1],
                            },
                        )
                    raise
                self._langfuse.update_current_span(
                    output={
                        "suggestions": [
                            {
                                "action": item.action,
                                "target_loop_id": item.target_loop_id,
                                "summary": item.summary,
                                "action_data": item.action_data,
                                "confidence": item.confidence,
                            }
                            for item, _ in valid_items
                        ],
                        "raw_responses": raw_responses,
                        "call_diagnostics": call_diagnostics,
                    }
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
                    action=SuggestedAction.ASK_COORDINATOR,
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

        # Checkpoint 2: the LLM call may have taken seconds. Another worker
        # could have started and finished in that window. Drop our (possibly
        # stale) suggestions rather than writing them on top of newer work.
        if not await is_current_generation(arq_pool, generation_thread_id, generation_token):
            logger.info(
                "next-action-agent superseded after LLM "
                "(thread=%s, token=%s, valid_items=%d) — dropping",
                generation_thread_id,
                generation_token,
                len(valid_items),
            )
            return

        seen_fingerprints: set[str] = {
            _suggestion_fingerprint(s.loop_id, s.action, s.action_data) for s in existing_pending
        }
        # The just-rejected suggestion isn't in pending (it's REJECTED), but we
        # must not let the agent re-emit it verbatim after a rejection re-run.
        if rejected_suggestion is not None:
            seen_fingerprints.add(
                _suggestion_fingerprint(
                    rejected_suggestion.loop_id,
                    rejected_suggestion.action,
                    rejected_suggestion.action_data,
                )
            )

        for item, target_loop in valid_items:
            loop_id = target_loop.id if target_loop else None
            fp = _suggestion_fingerprint(loop_id, item.action, item.action_data)
            if fp in seen_fingerprints:
                logger.info(
                    "dedup: skipping duplicate suggestion (action=%s, loop_id=%s)",
                    item.action,
                    loop_id,
                )
                continue
            seen_fingerprints.add(fp)

            suggestion = await self._suggestions.create_suggestion(
                coordinator_email=event.coordinator_email,
                gmail_message_id=msg.id,
                gmail_thread_id=msg.thread_id,
                item=item,
                reasoning=item.reasoning,
                loop_id=loop_id,
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

    # ------------------------------------------------------------------
    # Conversation pipeline
    # ------------------------------------------------------------------

    async def _act_with_messages(
        self,
        prompt: Any,
        messages: list[dict[str, str]],
        linked_loops: list[Loop],
        existing_pending: list[Suggestion],
        *,
        allow_retry: bool,
        prior_responses: list[str] | None = None,
        prior_diagnostics: list[dict] | None = None,
    ) -> tuple[list[tuple[SuggestionItem, Loop | None]], list[str], list[dict]]:
        """Run one LLM round-trip and validate the result.

        On batch/per-item errors, optionally append the assistant reply plus a
        user follow-up describing the errors and recurse once with
        ``allow_retry=False``.

        Returns ``(valid_items, raw_responses, call_diagnostics)``. The two
        list returns are accumulated across the initial call and any retry —
        each call appends one element to each list, in chronological order,
        so ``call_diagnostics[i]`` describes the LLM call that produced
        ``raw_responses[i]``. Both are written to the LangFuse span on
        success and attached to ``NextActionAgentError`` on failure.
        """
        prior_responses = prior_responses or []
        prior_diagnostics = prior_diagnostics or []
        response = await self._call_llm(prompt, messages)
        responses = [*prior_responses, response.content]
        diagnostics = [
            *prior_diagnostics,
            diagnostics_from_response(response, attempt=len(prior_responses)),
        ]
        try:
            result = self._parse_response(response.content)
        except NextActionAgentError as exc:
            if allow_retry:
                logger.warning(
                    "next-action-agent parse failed (%s) — retrying with follow-up "
                    "[finish_reason=%s, completion_tokens=%s, model=%s, "
                    "provider=%s, latency_ms=%.0f]",
                    exc,
                    response.finish_reason,
                    response.usage.get("completion_tokens"),
                    response.model,
                    response.provider,
                    response.latency_ms,
                )
                follow_up = (
                    "Your previous response could not be parsed. "
                    f"Error: {exc}\n\n"
                    "Please respond with a valid <suggestions>[...]</suggestions> "
                    "JSON array."
                )
                next_messages = [
                    *messages,
                    {"role": "assistant", "content": response.content},
                    {"role": "user", "content": follow_up},
                ]
                return await self._act_with_messages(
                    prompt,
                    next_messages,
                    linked_loops,
                    existing_pending,
                    allow_retry=False,
                    prior_responses=responses,
                    prior_diagnostics=diagnostics,
                )
            # Final failure on the retry attempt — attach both raw responses
            # AND per-call diagnostic metadata so the caller can put them on
            # the LangFuse span and Sentry context. Without diagnostics
            # ("finish_reason=stop, completion_tokens=140, ..."), we can't
            # distinguish natural-stop / length / content-filter failures
            # from each other — they all surface as the same parse error.
            logger.warning(
                "next-action-agent parse failed on retry (%s) — giving up "
                "[final_finish_reason=%s, completion_tokens=%s, model=%s, "
                "provider=%s, latency_ms=%.0f, total_attempts=%d]",
                exc,
                response.finish_reason,
                response.usage.get("completion_tokens"),
                response.model,
                response.provider,
                response.latency_ms,
                len(diagnostics),
            )
            raise NextActionAgentError(
                str(exc),
                raw_responses=responses,
                call_diagnostics=diagnostics,
            ) from exc

        valid_items, errors = self._validate_batch(
            result.suggestions, linked_loops, existing_pending
        )

        if errors and allow_retry:
            logger.info(
                "next-action-agent batch errors (%d) — retrying with follow-up", len(errors)
            )
            follow_up = build_error_followup(errors)
            next_messages = [
                *messages,
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": follow_up},
            ]
            return await self._act_with_messages(
                prompt,
                next_messages,
                linked_loops,
                existing_pending,
                allow_retry=False,
                prior_responses=responses,
                prior_diagnostics=diagnostics,
            )

        if errors:
            logger.warning(
                "next-action-agent retry still produced %d error(s) — keeping %d valid item(s)",
                len(errors),
                len(valid_items),
            )

        return valid_items, responses, diagnostics

    async def _call_llm(self, prompt: Any, messages: list[dict[str, str]]) -> LLMResponse:
        """Dispatch the conversation to the LLM service.

        Model config is sourced from the prompt's LangFuse config so prompt-
        version-pinned settings win.

        Updates the current LangFuse span's ``input`` to the messages list
        we're about to send. Retries append assistant + user-feedback turns
        to ``messages`` and recurse through this function, so the final span
        input reflects the complete conversation the model saw — including
        error follow-ups, coordinator responses, and rejection feedback.
        Without this update the trace shows our internal ``NextActionInput``
        dataclass rather than the actual prompt + retry history.
        """
        config: dict = prompt.config or {}
        model = config.get("model", DEFAULT_MODEL)
        temperature = config.get("temperature", 0.0)
        max_tokens = config.get("max_tokens", 4096)

        self._langfuse.update_current_span(input=messages)

        return await self._llm.complete(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def _build_initial_messages(
        self,
        prompt: Any,
        context_input: NextActionInput,
        *,
        coordinator_response: str | None,
        originating_suggestion: Suggestion | None,
        rejected_suggestion: Suggestion | None = None,
    ) -> list[dict[str, str]]:
        """Compile the prompt + (optionally) splice in the prior turn."""
        input_dict = context_input.model_dump()

        if isinstance(prompt, ChatPromptClient):
            compiled = prompt.compile(**input_dict)
            messages: list[dict[str, str]] = [dict(m) for m in compiled]
        else:
            compiled_str = prompt.compile(**input_dict)
            messages = [
                {"role": "system", "content": compiled_str},
                {"role": "user", "content": json.dumps(input_dict)},
            ]

        if coordinator_response is not None and originating_suggestion is not None:
            # Splice the prior turn into the conversation so the LLM sees the
            # question it had asked and the coordinator's answer.
            assistant_content = _reconstruct_prior_assistant(originating_suggestion)
            messages.append({"role": "assistant", "content": assistant_content})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"The coordinator responded with the following:\n{coordinator_response}"
                    ),
                }
            )
        elif rejected_suggestion is not None:
            # The coordinator dismissed the prior suggestion. We have no "why"
            # signal — just the fact of rejection. Tell the model to try a
            # materially different alternative or prefer no_action.
            assistant_content = _reconstruct_prior_assistant(rejected_suggestion)
            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user", "content": _REJECTION_FOLLOWUP_TEXT})

        return messages

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_response(self, content: str) -> ClassificationResult:
        """Extract the <suggestions> JSON array from the LLM response.

        Wraps the shared `parse_suggestions_envelope` helper to preserve
        the agent's `NextActionAgentError` contract — callers up the stack
        already catch that specific type.
        """
        from api.classifier.agent_runtime import SuggestionsParseError

        try:
            return parse_suggestions_envelope(content)
        except SuggestionsParseError as exc:
            raise NextActionAgentError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Context building
    # ------------------------------------------------------------------

    async def _build_context(
        self,
        event: EmailEvent,
        linked_loops: list[Loop],
        thread_messages: list[Message] | None = None,
    ) -> tuple[NextActionInput, list[Suggestion]]:
        msg = event.message

        if thread_messages:
            thread_history_xml = format_thread_history_xml(
                thread_messages, msg.id, event.coordinator_email
            )
        else:
            thread_history_xml = "<!-- No prior messages in this thread -->"

        date_str = format_llm_datetime()

        all_pending: list[Suggestion] = []
        pending_by_loop: dict[str, list[Suggestion]] = {}
        for lp in linked_loops:
            loop_pending = await self._suggestions.get_pending_for_loop(lp.id)
            pending_by_loop[lp.id] = loop_pending
            all_pending.extend(loop_pending)

        return (
            NextActionInput(
                date=date_str,
                thread_history=thread_history_xml,
                email=format_email_xml(msg, event.direction.value),
                loops=format_loops_xml(linked_loops, pending_by_loop),
            ),
            all_pending,
        )

    # ------------------------------------------------------------------
    # Guardrails
    # ------------------------------------------------------------------

    def _validate_batch(
        self,
        suggestions: list[SuggestionItem],
        linked_loops: list[Loop],
        existing_pending: list[Suggestion],
    ) -> tuple[list[tuple[SuggestionItem, Loop | None]], list[str]]:
        """Per-item validation + batch-level guardrails.

        Returns (valid_items, errors). Valid items may still be returned
        alongside errors; the caller decides whether to retry.
        """
        errors: list[str] = []
        valid: list[tuple[SuggestionItem, Loop | None]] = []

        pending_ids = {s.id for s in existing_pending}

        for item in suggestions:
            target_loop, loop_error = self._resolve_target_loop(item, linked_loops)
            if loop_error:
                errors.append(loop_error)
                continue
            if target_loop and not item.target_loop_id:
                item = item.model_copy(update={"target_loop_id": target_loop.id})

            item, item_error = self._apply_guardrails(item, pending_ids)
            if item_error:
                errors.append(item_error)
                continue

            valid.append((item, target_loop))

        # Batch-level: only one client draft per generation.
        client_drafts = sum(
            1
            for it, _ in valid
            if it.action == SuggestedAction.DRAFT_EMAIL
            and it.action_data.get("recipient_type") == "client"
        )
        if client_drafts > 1:
            errors.append(
                "Multiple client draft emails were proposed — combine them into a single "
                "client-facing email for this generation."
            )

        return valid, errors

    def _resolve_target_loop(
        self,
        item: SuggestionItem,
        linked_loops: list[Loop],
    ) -> tuple[Loop | None, str | None]:
        """Resolve the target loop for a suggestion. Returns (loop, error)."""
        loop_ids = [lp.id for lp in linked_loops]

        if item.target_loop_id:
            for loop in linked_loops:
                if loop.id == item.target_loop_id:
                    return loop, None
            return None, (
                f"target_loop_id '{item.target_loop_id}' does not match any linked loop. "
                f"Available loop IDs: {', '.join(loop_ids)}"
            )

        if len(linked_loops) == 1:
            return linked_loops[0], None

        return None, (
            f"target_loop_id is required (the agent only operates on linked threads). "
            f"Available loop IDs: {', '.join(loop_ids)}"
        )

    def _apply_guardrails(
        self,
        item: SuggestionItem,
        pending_ids: set[str],
    ) -> tuple[SuggestionItem, str | None]:
        """Per-item guardrails. Returns (item, error_message)."""
        if item.action not in _AGENT_ALLOWED_ACTIONS:
            allowed = ", ".join(sorted(a.value for a in _AGENT_ALLOWED_ACTIONS))
            return item, (
                f"Action '{item.action}' is not allowed for the next action agent — "
                f"allowed actions: {allowed}"
            )

        model_cls = ACTION_DATA_MODELS.get(item.action)
        if model_cls is None:
            return item, f"action '{item.action}' has no action_data schema"
        try:
            model_cls.model_validate(item.action_data)
        except ValidationError as e:
            return item, f"action_data for '{item.action}' is invalid: {e}"

        if not item.target_loop_id:
            return item, (
                f"action '{item.action}' requires target_loop_id "
                "(the agent always acts on a linked loop)"
            )

        if item.action == SuggestedAction.EXPIRE_SUGGESTION:
            target_sug_id = item.action_data.get("suggestion_id")
            if target_sug_id not in pending_ids:
                return item, (
                    f"expire_suggestion target '{target_sug_id}' is not a known pending "
                    "suggestion on this thread."
                )

        return item, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_REJECTION_FOLLOWUP_TEXT = (
    "The coordinator rejected the previous suggestion. "
    "We do NOT know why they rejected it.\n\n"
    "Propose a materially different alternative — for example: a different "
    "recipient, a different action type, or substantively different content. "
    "Do NOT repeat the rejected suggestion with cosmetic changes.\n\n"
    "If no materially different alternative is appropriate, emit no_action. "
    "It is better to do nothing than to propose another likely-bad option."
)


def _reconstruct_prior_assistant(suggestion: Suggestion) -> str:
    """Re-serialize a resolved suggestion as the prior assistant turn.

    Good enough for the coordinator-response flow: the model sees that it had
    asked the question. We don't try to reconstruct sibling suggestions from
    the same generation — keeping things simple and stateless.
    """
    payload: dict[str, Any] = {
        "classification": suggestion.classification.value
        if hasattr(suggestion.classification, "value")
        else suggestion.classification,
        "action": suggestion.action.value
        if hasattr(suggestion.action, "value")
        else suggestion.action,
        "confidence": suggestion.confidence,
        "summary": suggestion.summary,
        "reasoning": suggestion.reasoning or "",
        "target_loop_id": suggestion.loop_id,
        "action_data": suggestion.action_data or {},
    }
    return f"<suggestions>{json.dumps([payload])}</suggestions>"
