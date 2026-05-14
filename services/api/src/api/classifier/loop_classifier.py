"""LoopClassifier — handles emails on threads not yet linked to any loop.

Only processes inbound emails. Decides whether to:
- Create a new scheduling loop (CREATE_LOOP)
- Attach the thread to an existing loop (LINK_THREAD)
- Take no action (NO_ACTION)

After a CREATE_LOOP or LINK_THREAD auto-resolves, enqueues the
NextActionAgent to determine next steps on the now-linked thread.

I/O contract (v26+):
- Input is five template variables — coordinator, date, email,
  thread_history, active_loops — all rendered as XML-formatted strings via
  the `_simple` / `_xml` formatters tuned for the loop classifier's prompt.
- Output is a JSON array wrapped in `<suggestions>…</suggestions>` tags,
  preceded by the model's reasoning preamble.
- Retries (when every suggestion fails guardrails) ride the LLM
  conversation history — the agent appends the raw assistant turn plus a
  user-role error follow-up and recurses once with retry disabled.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import sentry_sdk
from langfuse.model import ChatPromptClient
from pydantic import ValidationError

from api.ai.langfuse_client import fetch_prompt
from api.ai.llm_service import DEFAULT_MODEL
from api.classifier.agent_runtime import (
    SuggestionsParseError,
    build_error_followup,
    diagnostics_from_response,
    parse_suggestions_envelope,
)
from api.classifier.formatters import (
    format_active_loops_xml,
    format_email_xml_simple,
    format_llm_datetime,
    format_thread_history_xml_simple,
)
from api.classifier.models import (
    ACTION_DATA_MODELS,
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

    from api.ai.llm_service import LLMResponse, LLMService
    from api.classifier.service import SuggestionService
    from api.gmail.hooks import EmailEvent
    from api.scheduling.models import Coordinator, Loop
    from api.scheduling.service import LoopService

logger = logging.getLogger(__name__)

LINK_THREAD_MIN_CONFIDENCE = 0.9

_CLASSIFIER_ALLOWED_ACTIONS = frozenset(
    {SuggestedAction.CREATE_LOOP, SuggestedAction.LINK_THREAD, SuggestedAction.NO_ACTION}
)

# Permitted client managers — mirrors the <client-managers> list in the
# scheduling-new-loop-classifier prompt. Lowercased for case-insensitive
# match (the model frequently lowercases names like RQuatroni).
_VALID_CM_EMAILS = frozenset(
    {
        "odinsmore@longridgepartners.com",
        "efreiberg@longridgepartners.com",
        "chirag@longridgepartners.com",
        "adam@longridgepartners.com",
        "alowe@longridgepartners.com",
        "hpark@longridgepartners.com",
        "rquatroni@longridgepartners.com",
        "jschulman@longridgepartners.com",
        "jsklar@longridgepartners.com",
        "matt@longridgepartners.com",
        "lthomas@longridgepartners.com",
        "nim@longridgepartners.com",
    }
)

_LRP_EMAIL_DOMAIN = "longridgepartners.com"

# Consumer email domains — clients always use a corporate domain, so any
# of these in `client_email` is grounds to reject the CREATE_LOOP and
# ask the model to re-read the email for the actual client contact.
# Not exhaustive — extend as we see false negatives in production traces.
_CONSUMER_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "ymail.com",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "msn.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "protonmail.com",
        "proton.me",
        "hey.com",
        "fastmail.com",
        "duck.com",
        "gmx.com",
        "mail.com",
        "zoho.com",
        "yandex.com",
        "yandex.ru",
        "qq.com",
        "163.com",
        "naver.com",
    }
)


def _email_domain(address: str) -> str:
    """Extract the lowercased domain from an email address. Empty string on malformed input."""
    if not address or "@" not in address:
        return ""
    return address.rsplit("@", 1)[1].strip().lower()


class LoopClassifierError(Exception):
    """Raised when the loop classifier cannot produce a usable response.

    Carries `raw_responses` and `call_diagnostics` (mirroring
    `NextActionAgentError`) so the caller can write the unparsed LLM
    output and per-call diagnostic metadata to the LangFuse span when
    validation fails.
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

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

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

        email_xml = format_email_xml_simple(msg, event.message_type.value)
        context_input = self._build_context(
            event,
            coord,
            active_loops,
            email_xml,
            event.thread_messages,
        )

        try:
            prompt = fetch_prompt(self._langfuse, "scheduling-new-loop-classifier")
            with self._langfuse.start_as_current_observation(
                name="loop-classifier",
            ):
                # Span input is set inside `_call_llm` to the live messages
                # list so retry follow-ups show up in the trace. Structured
                # context lives in metadata for reference.
                self._langfuse.update_current_span(
                    metadata={
                        "prompt_name": "scheduling-new-loop-classifier",
                        "prompt_version": prompt.version,
                        "prompt_labels": prompt.labels,
                        "context": context_input.model_dump(),
                    }
                )
                messages = self._build_initial_messages(prompt, context_input)
                try:
                    (
                        valid_items,
                        raw_responses,
                        call_diagnostics,
                    ) = await self._classify_with_messages(
                        prompt,
                        messages,
                        active_loops,
                        email_xml,
                        allow_retry=True,
                    )
                except LoopClassifierError as exc:
                    self._langfuse.update_current_span(
                        output={
                            "raw_responses": exc.raw_responses,
                            "call_diagnostics": exc.call_diagnostics,
                            "parse_error": str(exc),
                        }
                    )
                    if exc.call_diagnostics:
                        sentry_sdk.set_context(
                            "loop_classifier",
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
                            for item in valid_items
                        ],
                        "raw_responses": raw_responses,
                        "call_diagnostics": call_diagnostics,
                    }
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

        for item in valid_items:
            suggestion = await self._suggestions.create_suggestion(
                coordinator_email=event.coordinator_email,
                gmail_message_id=msg.id,
                gmail_thread_id=msg.thread_id,
                item=item,
                reasoning=item.reasoning,
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

    # ------------------------------------------------------------------
    # LLM round-trip + conversation-history retry
    # ------------------------------------------------------------------

    async def _classify_with_messages(
        self,
        prompt: Any,
        messages: list[dict[str, str]],
        active_loops: list[Loop],
        email_text: str,
        *,
        allow_retry: bool,
        prior_responses: list[str] | None = None,
        prior_diagnostics: list[dict] | None = None,
    ) -> tuple[list[SuggestionItem], list[str], list[dict]]:
        """Run one LLM round-trip and validate. Retry once on all-guardrail-fail.

        Returns `(valid_items, raw_responses, call_diagnostics)`. The two
        list returns accumulate across the initial call and any retry, in
        chronological order, so `call_diagnostics[i]` describes the LLM
        call that produced `raw_responses[i]`. Both end up on the
        LangFuse span on success and on `LoopClassifierError` on failure.
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
            result = parse_suggestions_envelope(response.content)
        except SuggestionsParseError as exc:
            if allow_retry:
                logger.warning(
                    "loop classifier parse failed (%s) — retrying with follow-up "
                    "[finish_reason=%s, completion_tokens=%s, model=%s, latency_ms=%.0f]",
                    exc,
                    response.finish_reason,
                    response.usage.get("completion_tokens"),
                    response.model,
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
                return await self._classify_with_messages(
                    prompt,
                    next_messages,
                    active_loops,
                    email_text,
                    allow_retry=False,
                    prior_responses=responses,
                    prior_diagnostics=diagnostics,
                )
            raise LoopClassifierError(
                str(exc),
                raw_responses=responses,
                call_diagnostics=diagnostics,
            ) from exc

        # Run each suggestion through guardrails — collect valid ones and
        # the human-readable per-item errors that the retry follow-up needs.
        valid_items: list[SuggestionItem] = []
        guardrail_errors: list[str] = []
        for item in result.suggestions:
            checked, error = self._apply_guardrails(item, active_loops, email_text)
            if error:
                guardrail_errors.append(error)
            else:
                valid_items.append(checked)

        # Retry only when every suggestion failed validation — partial
        # successes ship the valid items rather than discarding them.
        if guardrail_errors and not valid_items and allow_retry:
            logger.info(
                "loop classifier %d guardrail error(s) and 0 valid items — retrying",
                len(guardrail_errors),
            )
            sentry_sdk.set_tag("classifier.guardrail_retry", "true")
            follow_up = build_error_followup(guardrail_errors)
            next_messages = [
                *messages,
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": follow_up},
            ]
            return await self._classify_with_messages(
                prompt,
                next_messages,
                active_loops,
                email_text,
                allow_retry=False,
                prior_responses=responses,
                prior_diagnostics=diagnostics,
            )

        if guardrail_errors:
            logger.warning(
                "loop classifier retained %d valid item(s) alongside %d guardrail error(s)",
                len(valid_items),
                len(guardrail_errors),
            )

        return valid_items, responses, diagnostics

    async def _call_llm(self, prompt: Any, messages: list[dict[str, str]]) -> LLMResponse:
        """Direct dispatch to the LLM service.

        Model + temperature + max_tokens come from the prompt's LangFuse
        config so version-pinned settings win. Updates the current span's
        `input` to the live messages list — including retry follow-ups —
        so the trace reflects exactly what the model saw.
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
        context_input: LoopClassifierInput,
    ) -> list[dict[str, str]]:
        """Compile the LangFuse prompt with the typed input."""
        input_dict = context_input.model_dump()
        if isinstance(prompt, ChatPromptClient):
            compiled = prompt.compile(**input_dict)
            return [dict(m) for m in compiled]
        compiled_str = prompt.compile(**input_dict)
        return [{"role": "system", "content": compiled_str}]

    # ------------------------------------------------------------------
    # Context building
    # ------------------------------------------------------------------

    def _build_context(
        self,
        event: EmailEvent,
        coord: Coordinator | None,
        active_loops: list[Loop],
        email_xml: str,
        thread_messages: list[Any] | None = None,
    ) -> LoopClassifierInput:
        if thread_messages:
            thread_history_xml = format_thread_history_xml_simple(
                thread_messages, event.message.id, event.coordinator_email
            )
        else:
            thread_history_xml = "No prior messages in this thread."

        coordinator_name = _resolve_coordinator_name(event, coord)
        coordinator_str = f"{coordinator_name} <{event.coordinator_email}>"
        date_str = format_llm_datetime()

        return LoopClassifierInput(
            coordinator=coordinator_str,
            date=date_str,
            email=email_xml,
            thread_history=thread_history_xml,
            active_loops=format_active_loops_xml(active_loops),
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

    # ------------------------------------------------------------------
    # Guardrails
    # ------------------------------------------------------------------

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
        if item.action == SuggestedAction.CREATE_LOOP:
            cand_name = (item.action_data.get("candidate_name") or "").strip()
            if not cand_name or cand_name.lower() == "unknown candidate":
                return (
                    item.model_copy(update={"action": SuggestedAction.NO_ACTION}),
                    "CREATE_LOOP candidate_name must be a real name, not 'Unknown Candidate'. "
                    "Re-read the email to extract the candidate's actual name.",
                )

            # 7. CREATE_LOOP cm_email must be on the canonical CM list.
            # The CM is always present (the LRP person managing the client
            # relationship) — even on automated ATS emails the recruiter/CM
            # is on the recipient list.
            cm_email = (item.action_data.get("cm_email") or "").strip().lower()
            if cm_email not in _VALID_CM_EMAILS:
                return (
                    item,
                    f"CREATE_LOOP cm_email '{cm_email}' is not on the permitted "
                    f"client-manager list. The CM MUST be one of: "
                    f"{', '.join(sorted(_VALID_CM_EMAILS))}. Re-read the email and "
                    f"pick the LRP person on the thread who is on this list.",
                )

            # 8. CREATE_LOOP client_contact validity — only enforced when the
            # model actually produced a client_name / client_email. Automated
            # ATS notifications (e.g. Greenhouse) often arrive without a
            # named client contact; we accept those and let the coordinator
            # fill it in downstream rather than blocking loop creation.
            client_name = (item.action_data.get("client_name") or "").strip()
            client_email = (item.action_data.get("client_email") or "").strip()

            if client_name and cand_name and client_name.lower() == cand_name.lower():
                return (
                    item,
                    f"CREATE_LOOP client_name '{client_name}' is the same as "
                    f"candidate_name '{cand_name}'. The client contact is the "
                    f"external party requesting the interview — never the candidate.",
                )

            if client_email:
                if "@" not in client_email:
                    return (
                        item,
                        f"CREATE_LOOP client_email '{client_email}' is malformed. "
                        f"Either extract the actual client contact's email or "
                        f"omit the field if it isn't in the thread.",
                    )
                client_domain = _email_domain(client_email)
                if client_domain == _LRP_EMAIL_DOMAIN:
                    return (
                        item,
                        f"CREATE_LOOP client_email '{client_email}' is an LRP "
                        f"address. The client is always the EXTERNAL party — pick "
                        f"someone with a corporate (non-@{_LRP_EMAIL_DOMAIN}) "
                        f"email, or omit client_email if no external contact is on the thread.",
                    )
                if client_domain in _CONSUMER_EMAIL_DOMAINS:
                    return (
                        item,
                        f"CREATE_LOOP client_email '{client_email}' uses a consumer "
                        f"email provider ({client_domain}). Clients use a corporate "
                        f"domain; a consumer address usually means the model "
                        f"extracted the candidate or another non-client party. "
                        f"Either pick the real client contact or omit the field.",
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
        extracted_client = extraction.client_company.lower().strip() if extraction else ""
        target_client = target_company.lower().strip() if target_company else ""
        if (
            extraction
            and target_company
            and extracted_client not in target_client
            and target_client not in extracted_client
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
