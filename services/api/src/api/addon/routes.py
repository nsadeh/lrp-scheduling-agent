"""Google Workspace Add-on HTTP endpoints.

Google POSTs to these routes when a coordinator interacts with the Gmail sidebar.
Token verification is applied at the router level via Depends().
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from api.addon.directory import search_directory
from api.addon.models import (
    ActionResponse,
    AddonRequest,
    CardResponse,
    PushCard,
    SuggestionItem,
    Suggestions,
    SuggestionsResponse,
    UpdateCard,
)
from api.app_state import (
    get_draft_service,
    get_gmail,
    get_langfuse,
    get_llm,
    get_overview_service,
    get_redis,
    get_scheduling,
)
from api.classifier.create_loop_extractor import (
    ExtractCreateLoopInput,
    extract_create_loop_fields,
)
from api.classifier.formatters import format_thread_history
from api.classifier.models import CreateLoopExtraction, SuggestedAction, SuggestionStatus
from api.gmail.exceptions import (
    GmailScopeError,
    GmailUserNotAuthorizedError,
    GmailValidationError,
)
from api.gmail.forward import build_forwarded_body, prefix_forward_subject
from api.overview.cards import build_overview
from api.overview.service import OverviewService
from api.scheduling.cards import (
    build_auth_required,
    build_contextual_unlinked,
    build_create_loop_form,
    build_error_card,
    build_loop_pending_card,
)
from api.scheduling.models import StageState
from api.scheduling.service import LoopService  # noqa: TC001

logger = logging.getLogger(__name__)

addon_router = APIRouter(
    prefix="/addon",
    tags=["addon"],
)


def _get_overview_service(request: Request) -> OverviewService:
    svc = get_overview_service(request)
    if svc is None:
        # Lazily create from the db pool
        svc = OverviewService(request.app.state.db)
        request.app.state.overview_service = svc
    return svc


def _get_base_url(request: Request) -> str:
    """Extract the base URL from the current request (e.g. https://xxx.ngrok-free.app)."""
    return str(request.url).rsplit("/addon/", 1)[0]


def _get_user_email(body: AddonRequest) -> str:
    """Extract coordinator email from the add-on request.

    Only ``userIdToken`` (Google-signed JWT with the coordinator's email
    claim) is trusted as a user identity. ``systemIdToken`` is explicitly
    NOT used here even though it has an ``email`` claim — that token's
    email is the add-on framework's service-account identity, used to
    prove the request came from Google (see ``addon/auth.py``), not to
    identify the human coordinator. Falling back to it silently
    masquerades the wrong principal: see the regression where adding a
    sensitive scope to the deployment manifest's ``oauthScopes`` caused
    Google to suppress ``userIdToken``, the silent fallback used the
    service-account email, and the "Authorize Gmail Access" button then
    asked the coordinator to log in as that service account.

    Falls back to ``userOAuthToken`` → Google's userinfo endpoint as a
    last resort (still a user-identifying token). Raises ``ValueError``
    if no user-identifying token is present, so the failure is loud
    instead of silently using the wrong identity.
    """
    import base64
    import json

    auth = body.authorization_event_object
    if not auth:
        logger.error("No authorizationEventObject in request body")
        raise ValueError("No authorizationEventObject in add-on request")

    # User-identifying tokens only. Order: userIdToken (preferred),
    # then the userinfo-endpoint fallback below for userOAuthToken.
    token_sources = [
        ("userIdToken", auth.user_id_token),
    ]

    # Log what we received for debugging — surface presence of
    # systemIdToken too because its absence/presence is diagnostic for
    # add-on consent issues, even though we never read its email.
    available = [name for name, val in token_sources if val]
    logger.info(
        "Available tokens in request: user-identifying=%s, systemIdToken_present=%s",
        available,
        bool(auth.system_id_token),
    )

    for name, token_value in token_sources:
        if not token_value:
            continue
        try:
            payload = token_value.split(".")[1]
            payload += "=" * (4 - len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            if "email" in claims:
                logger.info("Got user email from %s: %s", name, claims["email"])
                return claims["email"]
        except Exception:
            logger.warning("Could not decode %s for email", name, exc_info=True)

    # Fallback: if we have a userOAuthToken, call Google's userinfo endpoint
    if auth.user_oauth_token:
        try:
            import requests as http_requests

            resp = http_requests.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {auth.user_oauth_token}"},
                timeout=5,
            )
            if resp.ok:
                email = resp.json().get("email")
                if email:
                    logger.info("Got user email from userinfo endpoint: %s", email)
                    return email
                logger.warning("userinfo 200 but no email in body: %s", resp.text[:500])
            else:
                logger.warning(
                    "userinfo rejected userOAuthToken: status=%s body=%s",
                    resp.status_code,
                    resp.text[:500],
                )
        except Exception:
            logger.warning("Failed to fetch userinfo", exc_info=True)

    logger.error(
        "No email found in any token. Available: %s",
        [name for name, val in token_sources if val],
    )
    raise ValueError("Could not determine coordinator email from add-on request")


def _get_form_value(body: AddonRequest, field: str) -> str | None:
    """Extract a form input value from the add-on request."""
    if not body.common_event_object or not body.common_event_object.form_inputs:
        return None
    fi = body.common_event_object.form_inputs.get(field)
    if fi and isinstance(fi, dict):
        # Google sends form inputs as {"fieldName": {"stringInputs": {"value": ["val"]}}}
        string_inputs = fi.get("stringInputs", {})
        values = string_inputs.get("value", [])
        return values[0] if values else None
    return None


def _get_param(body: AddonRequest, key: str) -> str | None:
    """Extract a parameter from the action invocation."""
    if not body.common_event_object or not body.common_event_object.parameters:
        return None
    return body.common_event_object.parameters.get(key)


# Substrings that mark a form input as a directory-autocomplete field.
# Matches the manual create-loop form (``recruiter_name``,
# ``recruiter_email``) and both JIT roles on draft cards
# (``jit_recruiter_*_<sug_id>``, ``jit_cm_*_<sug_id>``). CM and recruiter
# share the directory endpoint because both are LRP Workspace members.
_AUTOCOMPLETE_DIRECTORY_MARKERS = (
    "recruiter_name",
    "recruiter_email",
    "jit_cm_name",
    "jit_cm_email",
)


def _extract_autocomplete_query(body: AddonRequest) -> str:
    """Pull the currently-typed text out of an autoCompleteAction request.

    HTTP-based add-ons put every form input's value in
    ``commonEventObject.formInputs[<name>]``, with no built-in indicator
    of which field triggered the autocomplete. We resolve the triggering
    field via the ``autocomplete_field`` action parameter that
    ``build_recruiter_inputs`` bakes into the per-field autocomplete
    action; that's the only reliable signal when multiple directory
    inputs are on screen.

    Falls back to a marker-substring scan for the create-loop form
    (which doesn't pass ``autocomplete_field``) and finally to
    ``parameters["query"]`` for older tests.
    """
    inputs = (
        body.common_event_object.form_inputs
        if body.common_event_object and body.common_event_object.form_inputs
        else {}
    )

    # Preferred: the per-field action passes its own field name.
    target_field = _get_param(body, "autocomplete_field")
    if target_field:
        val = _get_form_value(body, target_field) or ""
        val = val.strip()
        logger.info(
            "directory/search: query from autocomplete_field=%r (len=%d)",
            target_field,
            len(val),
        )
        return val

    # Fallback: marker scan for the create-loop form's shared action.
    candidate_fields: list[str] = []
    for field_name in inputs:
        if any(marker in field_name for marker in _AUTOCOMPLETE_DIRECTORY_MARKERS):
            candidate_fields.append(field_name)

    for field_name in candidate_fields:
        val = _get_form_value(body, field_name)
        if val and val.strip():
            logger.info(
                "directory/search: extracted query from field %r (len=%d)",
                field_name,
                len(val.strip()),
            )
            return val.strip()

    # Final fallback: static parameter form (used by older tests).
    param_q = _get_param(body, "query") or ""
    if param_q.strip():
        return param_q.strip()

    logger.info(
        "directory/search: empty query — inspected fields=%s, all_form_keys=%s",
        candidate_fields,
        list(inputs.keys()),
    )
    return ""


_GMAIL_CONTEXTUAL_ID_RE = re.compile(r"^(?:thread-f|msg-f):(\d+)$")

# Format used in autocomplete dropdown items and parsed back out on selection.
# Matching is anchored and tolerates spaces around the angle brackets. Both
# the name and email groups exclude angle brackets so a crafted input like
# ``"A <script> B <real@x.com>"`` cannot smuggle angle-bracket content into
# the persisted name field.
_NAME_EMAIL_RE = re.compile(r"^\s*([^<>]+?)\s*<\s*([^<>\s]+@[^<>\s]+)\s*>\s*$")


def format_directory_suggestion(display_name: str, email: str) -> str:
    """Encode a directory result as a single dropdown text entry.

    Picked back apart by ``parse_name_email`` after the coordinator selects.
    Kept as a module-level function so tests can call it without importing
    the routes module's HTTP plumbing.
    """
    name = (display_name or "").strip() or email.split("@")[0]
    return f"{name} <{email}>"


def parse_name_email(text: str) -> tuple[str, str] | None:
    """Parse ``"Name <email>"`` back into ``(name, email)``. None if no match."""
    if not text:
        return None
    m = _NAME_EMAIL_RE.match(text)
    if m is None:
        return None
    return m.group(1), m.group(2)


def _normalize_gmail_id(raw_id: str | None) -> tuple[str | None, bool]:
    """Convert Google contextual-trigger IDs to Gmail API hex format.

    Google's contextual triggers send IDs like 'thread-f:1862729221917227576'
    (decimal with prefix), but the Gmail API and our database use the hex
    form '19d9be5bb11dba38'. They're the same number in different bases.

    Some Workspace editions send compound IDs like
    'thread-f:DEC|msg-f:DEC' — we split on '|' and use the first segment.

    Returns ``(normalized_id, was_rewritten)`` so callers can decide whether
    to attempt a Gmail API fallback when the rewritten ID misses the DB.
    """
    if not raw_id:
        return None, False
    segment = raw_id.split("|")[0]
    was_compound = segment != raw_id
    if was_compound:
        logger.warning("Compound Gmail ID received: %s — using first segment", raw_id)
    m = _GMAIL_CONTEXTUAL_ID_RE.match(segment)
    if m:
        return hex(int(m.group(1)))[2:], was_compound  # strip '0x' prefix
    return raw_id, False


async def _resolve_thread_id_via_gmail(
    gmail_client, user_email: str, message_id: str
) -> str | None:
    """Fetch the canonical threadId via domain-wide delegation.

    Only invoked when ``_normalize_gmail_id`` rewrote a compound ID that
    didn't match anything in the database — not on every unlinked thread.
    """
    try:
        message = await gmail_client.get_message(user_email, message_id)
        return message.thread_id
    except Exception:
        logger.exception("Gmail API fallback failed for message %s", message_id)
        return None


async def _check_gmail_auth(request: Request, user_email: str) -> CardResponse | None:
    """Check if the coordinator has stored Gmail credentials.

    Returns an auth-required card if not, or None if authorized.
    """
    gmail = get_gmail(request)
    if not gmail:
        return None  # GmailClient not configured — skip auth check
    has = await gmail.has_token(user_email)
    if has:
        return None
    # Build the OAuth authorization URL
    base = str(request.url).rsplit("/addon/", 1)[0]
    auth_url = f"{base}/addon/oauth/start?user_email={user_email}"
    return build_auth_required(auth_url)


def _ensure_action_url(request: Request) -> None:
    """Derive the /addon/action URL from the current request URL and set it for card builders."""
    from api.scheduling.cards import set_action_url

    # Current URL is e.g. https://xxx.ngrok-free.app/addon/homepage
    # We need https://xxx.ngrok-free.app/addon/action
    base = str(request.url).rsplit("/addon/", 1)[0]
    set_action_url(f"{base}/addon/action")


async def _build_refreshed_overview(request: Request, email: str) -> CardResponse:
    """Fetch latest suggestions and return a fresh overview card."""
    overview_svc = _get_overview_service(request)
    base_url = _get_base_url(request)
    groups = await overview_svc.get_overview_data(email)
    return build_overview(groups, base_url=base_url)


async def _build_thread_overview_or_placeholder(
    request: Request,
    email: str,
    thread_id: str | None,
    *,
    message_id: str | None = None,
) -> CardResponse:
    """Render suggestions for ``thread_id``, or a thread-anchored placeholder
    when none exist yet (e.g. agent still enqueued).

    Never widens to the global overview when a thread is in scope — that's
    what produced the stale-suggestion bug after manual create-loop, where
    the new loop had no suggestions yet and the global view showed an
    unrelated loop's pending items.
    """
    if not thread_id:
        return await _build_refreshed_overview(request, email)
    overview_svc = _get_overview_service(request)
    base_url = _get_base_url(request)
    groups = await overview_svc.get_thread_overview_data(thread_id, email)
    if groups:
        return build_overview(groups, base_url=base_url)
    return build_loop_pending_card(thread_id, message_id=message_id)


def _as_push(response: CardResponse) -> CardResponse:
    """Convert updateCard navigations to pushCard for initial triggers."""
    navigations = [
        PushCard(push_card=nav.update_card) if isinstance(nav, UpdateCard) else nav
        for nav in response.action.navigations
    ]
    return CardResponse(action=ActionResponse(navigations=navigations))


@addon_router.post("/homepage")
async def addon_homepage(body: AddonRequest, request: Request) -> dict:
    """Homepage trigger — suggestion-centric overview."""
    _ensure_action_url(request)

    # Debug: log what Google actually sends in authorizationEventObject
    if body.authorization_event_object:
        auth_dump = body.authorization_event_object.model_dump(exclude_none=True)
        has_id = body.authorization_event_object.user_id_token is not None
        has_oauth = body.authorization_event_object.user_oauth_token is not None
        logger.info(
            "Homepage auth keys: %s, userIdToken: %s, userOAuthToken: %s, authorizedScopes: %s",
            list(auth_dump.keys()),
            has_id,
            has_oauth,
            auth_dump.get("authorizedScopes") or auth_dump.get("authorized_scopes"),
        )
    else:
        logger.warning("Homepage request has NO authorizationEventObject")

    email = _get_user_email(body)

    auth_card = await _check_gmail_auth(request, email)
    if auth_card:
        return _as_push(auth_card).model_dump(by_alias=True, exclude_none=True)

    card = await _build_refreshed_overview(request, email)
    return _as_push(card).model_dump(by_alias=True, exclude_none=True)


@addon_router.post("/on-message")
async def addon_on_message(body: AddonRequest, request: Request) -> dict:
    """Contextual trigger — show suggestions for this thread, or create prompt."""
    _ensure_action_url(request)
    email = _get_user_email(body)
    logger.info("on-message: coordinator=%s", email)

    auth_card = await _check_gmail_auth(request, email)
    if auth_card:
        logger.info("on-message: returning auth-required card for %s", email)
        return _as_push(auth_card).model_dump(by_alias=True, exclude_none=True)

    thread_id = None
    message_id = None
    id_was_rewritten = False
    if body.gmail:
        thread_id, id_was_rewritten = _normalize_gmail_id(body.gmail.thread_id)
        message_id, _ = _normalize_gmail_id(body.gmail.message_id)

    logger.info(
        "on-message: thread_id=%s, message_id=%s, email=%s",
        thread_id,
        message_id,
        email,
    )

    if not thread_id:
        # No thread context — show full overview
        card = await _build_refreshed_overview(request, email)
        return _as_push(card).model_dump(by_alias=True, exclude_none=True)

    # Show suggestions filtered to this thread
    overview_svc = _get_overview_service(request)
    base_url = _get_base_url(request)
    groups = await overview_svc.get_thread_overview_data(thread_id, email)

    logger.info(
        "on-message: thread_id=%s, groups=%d, has_suggestions=%s",
        thread_id,
        len(groups),
        bool(groups),
    )

    if groups:
        card = build_overview(groups, base_url=base_url)
    else:
        # No suggestions for this thread — check if it's linked to a loop
        svc = get_scheduling(request)
        loop = await svc.find_loop_by_thread(thread_id)
        logger.info(
            "on-message: thread_id=%s, linked_loop=%s",
            thread_id,
            loop.id if loop else None,
        )
        gmail = get_gmail(request)
        if not loop and id_was_rewritten and gmail and message_id:
            api_thread_id = await _resolve_thread_id_via_gmail(gmail, email, message_id)
            if api_thread_id and api_thread_id != thread_id:
                logger.info(
                    "on-message: API fallback resolved thread_id=%s (was %s)",
                    api_thread_id,
                    thread_id,
                )
                groups = await overview_svc.get_thread_overview_data(api_thread_id, email)
                if groups:
                    card = build_overview(groups, base_url=base_url)
                    return _as_push(card).model_dump(by_alias=True, exclude_none=True)
                loop = await svc.find_loop_by_thread(api_thread_id)

        if loop:
            card = build_loop_pending_card(thread_id, message_id=message_id)
        else:
            card = build_contextual_unlinked(thread_id, message_id=message_id)

    return _as_push(card).model_dump(by_alias=True, exclude_none=True)


@addon_router.post("/action")
async def addon_action(body: AddonRequest, request: Request) -> dict:
    """Action handler — dispatches based on action_name parameter.

    For HTTP-based add-ons, Google POSTs to the URL in the Action's `function` field.
    We pass the logical action name as the `action_name` parameter.
    """
    _ensure_action_url(request)

    svc = get_scheduling(request)
    email = _get_user_email(body)

    # Read action name from parameters (set by our card builders)
    fn = _get_param(body, "action_name")
    # Fallback to invokedFunction for backward compatibility
    if not fn and body.common_event_object:
        fn = body.common_event_object.invoked_function

    handler = _ACTION_HANDLERS.get(fn or "", _handle_unknown)
    try:
        card = await handler(body, svc, email, request=request)
    except (GmailScopeError, GmailUserNotAuthorizedError) as exc:
        # Both share the same fix from the coordinator's POV: visit
        # /addon/oauth/start to (re-)consent. Scope errors fire when an
        # existing token is missing scopes; UserNotAuthorized fires when
        # there's no token at all (rare in the action path because
        # homepage/on-message gate it earlier, but possible if Google
        # routes a stale action target to an unauthorized coordinator).
        logger.warning("Gmail auth error in action %s: %s", fn, exc)
        base = str(request.url).rsplit("/addon/", 1)[0]
        auth_url = f"{base}/addon/oauth/start?user_email={email}"
        card = build_auth_required(auth_url)
    except GmailValidationError as exc:
        logger.warning("Gmail validation error in action %s: %s", fn, exc)
        card = build_error_card(str(exc))
    return card.model_dump(by_alias=True, exclude_none=True)


@addon_router.post("/directory/search")
async def addon_directory_search(body: AddonRequest, request: Request) -> dict:
    """Autocomplete callback for recruiter fields in the create-loop form.

    Wired from ``TextInput.autoCompleteAction``. Google POSTs here after
    its own debounce. The currently-typed text lives in
    ``commonEventObject.formInputs[<inputName>]`` — the same path used by
    every other field value in HTTP add-ons — because autoCompleteAction
    doesn't populate a separate ``query`` parameter. We accept input from
    either recruiter field (name or email), whichever the coordinator is
    typing into. Returns a flat ``SuggestionsResponse`` with
    ``"Name <email>"`` strings drawn from the calling coordinator's
    Workspace directory via the People API.
    """
    email = _get_user_email(body)
    query = _extract_autocomplete_query(body)
    if not query:
        return _empty_suggestions().model_dump(by_alias=True, exclude_none=True)

    gmail = get_gmail(request)
    if gmail is None:
        logger.warning("directory/search: no gmail client configured")
        return _empty_suggestions().model_dump(by_alias=True, exclude_none=True)

    try:
        creds = await gmail._token_store.load_credentials(email)
    except GmailScopeError as exc:
        # Suggestions can't surface the auth card. show_create_form
        # pre-checks scopes, so hitting this branch means either a race
        # (coordinator already had the form open when the scope list
        # grew) or the pre-check was bypassed. Log at WARNING so this
        # doesn't silently swallow real bugs like "coordinator's token
        # actually lacks directory.readonly."
        logger.warning(
            "directory/search: scope error for %s (missing=%s) — returning empty",
            email,
            getattr(exc, "missing_scopes", None),
        )
        return _empty_suggestions().model_dump(by_alias=True, exclude_none=True)
    except Exception:
        logger.exception("directory/search: failed to load creds for %s", email)
        return _empty_suggestions().model_dump(by_alias=True, exclude_none=True)

    try:
        people = await search_directory(creds, query, page_size=10)
    except Exception:
        logger.exception("directory/search: People API call failed for %s", email)
        return _empty_suggestions().model_dump(by_alias=True, exclude_none=True)

    items = [
        SuggestionItem(text=format_directory_suggestion(p.display_name, p.email)) for p in people
    ]
    logger.info(
        "directory/search: query_len=%d, results=%d, coordinator=%s",
        len(query),
        len(items),
        email,
    )
    response = SuggestionsResponse(auto_complete=Suggestions(items=items))
    payload = response.model_dump(by_alias=True, exclude_none=True)
    logger.info("directory/search: response_body=%s", payload)
    return payload


def _empty_suggestions() -> SuggestionsResponse:
    return SuggestionsResponse(auto_complete=Suggestions(items=[]))


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


async def _fetch_recruiter_photo_url(
    *,
    request: Request | None,
    svc: LoopService,
    coordinator_email: str,
    recruiter_email: str,
) -> str | None:
    """Look up the recruiter's Workspace directory photo by email, else None.

    Returns None fast when we already have a contact row for this email
    (existing photo_url is preserved by the service layer's dedup path) or
    when the directory lookup fails for any reason — the feature must
    degrade gracefully to "no avatar" rather than block loop creation.
    """
    if not recruiter_email:
        return None
    existing = await svc.get_contact_by_email(recruiter_email, role="recruiter")
    if existing is not None:
        return None

    if request is None:
        return None
    gmail = get_gmail(request)
    if gmail is None:
        return None
    try:
        creds = await gmail._token_store.load_credentials(coordinator_email)
        results = await search_directory(creds, recruiter_email, page_size=5)
    except Exception:
        logger.warning(
            "photo-url lookup failed for %s (recruiter=%s)",
            coordinator_email,
            recruiter_email,
            exc_info=True,
        )
        return None
    match = next(
        (p for p in results if p.email.lower() == recruiter_email.lower()),
        None,
    )
    return match.photo_url if match else None


# Hard cap on the create-loop extractor round-trip. Beyond this we give up
# and render the form with deterministic prefill only (RFC R1).
_EXTRACTOR_TIMEOUT_SECONDS = 10.0

_CREATE_LOOP_BANNER = "Filled in from the thread — please review before creating."


def _merge_prefill(
    deterministic: CreateLoopExtraction, extracted: CreateLoopExtraction
) -> CreateLoopExtraction:
    """Overlay extractor values on top of deterministic prefill.

    Deterministic values (from the current message's ``from_`` / ``cc[0]``)
    win for any field they populate — the model should never overwrite a
    signal we already have. Extractor fills the blanks, including fields
    with no deterministic counterpart (candidate_name, client_company).
    """
    return CreateLoopExtraction(
        candidate_name=deterministic.candidate_name or extracted.candidate_name,
        client_name=deterministic.client_name or extracted.client_name,
        client_email=deterministic.client_email or extracted.client_email,
        client_company=deterministic.client_company or extracted.client_company,
        cm_name=deterministic.cm_name or extracted.cm_name,
        cm_email=deterministic.cm_email or extracted.cm_email,
        recruiter_name=deterministic.recruiter_name or extracted.recruiter_name,
        recruiter_email=deterministic.recruiter_email or extracted.recruiter_email,
    )


async def _run_create_loop_extractor(
    *,
    request: Request | None,
    svc: LoopService,
    email: str,
    gmail_thread_id: str | None,
    message_id: str | None,
) -> CreateLoopExtraction | None:
    """Invoke the typed LLM extractor with a hard timeout.

    Returns None on timeout, error, or when prerequisites (request state,
    Gmail client, thread id) are missing. Callers fall back to deterministic
    prefill when this returns None.
    """
    if request is None or not gmail_thread_id or svc._gmail is None:
        return None

    llm = get_llm(request)
    langfuse = get_langfuse(request)
    if llm is None or langfuse is None:
        return None

    try:
        thread = await svc._gmail.get_thread(email, gmail_thread_id)
        # Pass "" for current_message_id so NO message is excluded — the
        # extractor prompt has no separate {{email}} section, so it must see
        # every message in the thread, including the one the coordinator is
        # looking at (which is usually THE message we want to extract from).
        formatted = format_thread_history(thread.messages, current_message_id="")
        return await asyncio.wait_for(
            extract_create_loop_fields(
                llm=llm,
                langfuse=langfuse,
                data=ExtractCreateLoopInput(
                    thread_history=formatted,
                    coordinator_email=email,
                ),
            ),
            timeout=_EXTRACTOR_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning(
            "create-loop extractor failed for thread %s — falling back to deterministic prefill",
            gmail_thread_id,
            exc_info=True,
        )
        return None


async def _handle_show_create_form(body: AddonRequest, svc: LoopService, email: str, **kwargs):
    request = kwargs.get("request")
    # Pre-check coordinator's directory.readonly scope here, BEFORE the
    # autocomplete callbacks fire on the rendered form. autoCompleteAction
    # responses can't surface the auth-required card, so we bounce to
    # re-consent at form-entry time. The action wrapper catches both
    # GmailScopeError (existing token missing scopes) and
    # GmailUserNotAuthorizedError (no token at all) and shows
    # build_auth_required, so we let either propagate.
    gmail = get_gmail(request) if request else None
    if gmail is not None:
        await gmail._token_store.load_credentials(email)

    gmail_thread_id = _get_param(body, "gmail_thread_id")
    message_id = _get_param(body, "message_id")

    # Prefill params passed from the overview suggestion card (classifier path)
    # short-circuit AI extraction — the classifier already did this work and
    # its output is on action_data.
    has_suggestion_prefill = any(
        _get_param(body, f"prefill_{field}")
        for field in (
            "candidate_name",
            "client_name",
            "client_email",
            "client_company",
            "recruiter_name",
            "recruiter_email",
            "cm_name",
            "cm_email",
        )
    )
    prefill = CreateLoopExtraction(
        candidate_name=_get_param(body, "prefill_candidate_name") or None,
        client_name=_get_param(body, "prefill_client_name") or None,
        client_email=_get_param(body, "prefill_client_email") or None,
        client_company=_get_param(body, "prefill_client_company") or None,
        recruiter_name=_get_param(body, "prefill_recruiter_name") or None,
        recruiter_email=_get_param(body, "prefill_recruiter_email") or None,
        cm_name=_get_param(body, "prefill_cm_name") or None,
        cm_email=_get_param(body, "prefill_cm_email") or None,
    )
    gmail_subject = None
    banner: str | None = None

    # Deterministic prefill from the current message: from_ → client contact,
    # cc[0] → client manager. Only applies on the manual path (no prefill
    # from a suggestion).
    if not has_suggestion_prefill and message_id and svc._gmail:
        try:
            msg = await svc._gmail.get_message(email, message_id)
            if msg.from_:
                prefill.client_name = prefill.client_name or (msg.from_.name or None)
                prefill.client_email = prefill.client_email or msg.from_.email
            if msg.cc:
                prefill.cm_name = prefill.cm_name or (msg.cc[0].name or None)
                prefill.cm_email = prefill.cm_email or msg.cc[0].email
            gmail_subject = msg.subject
        except Exception:
            logger.warning("Could not fetch message %s for pre-fill", message_id, exc_info=True)

    # AI extraction: only on the manual path. When the user arrived from a
    # classifier suggestion, that suggestion's action_data already populated
    # prefill_* — re-running extraction wastes LLM cost.
    if not has_suggestion_prefill:
        extracted = await _run_create_loop_extractor(
            request=request,
            svc=svc,
            email=email,
            gmail_thread_id=gmail_thread_id,
            message_id=message_id,
        )
        if extracted is not None:
            prefill = _merge_prefill(deterministic=prefill, extracted=extracted)
            banner = _CREATE_LOOP_BANNER

    # If a pre-filled email matches an existing contact, prefer the stored
    # name/company so the form shows what will actually be persisted on
    # submit — the dedup logic in find_or_create_contact keeps the stored
    # row untouched, so showing the classifier-suggested name here would
    # mislead the coordinator.
    if prefill.client_email:
        existing_client = await svc.get_client_contact_by_email(prefill.client_email)
        if existing_client is not None:
            prefill.client_name = existing_client.name
            if existing_client.company:
                prefill.client_company = existing_client.company
    if prefill.recruiter_email:
        existing_recruiter = await svc.get_contact_by_email(
            prefill.recruiter_email, role="recruiter"
        )
        if existing_recruiter is not None:
            prefill.recruiter_name = existing_recruiter.name
    if prefill.cm_email:
        existing_cm = await svc.get_contact_by_email(prefill.cm_email, role="client_manager")
        if existing_cm is not None:
            prefill.cm_name = existing_cm.name

    # Pass suggestion_id through so create_loop can resolve it
    suggestion_id = _get_param(body, "suggestion_id")

    return build_create_loop_form(
        gmail_thread_id=gmail_thread_id,
        gmail_subject=gmail_subject,
        gmail_message_id=message_id,
        prefill_candidate_name=prefill.candidate_name,
        prefill_client_name=prefill.client_name,
        prefill_client_email=prefill.client_email,
        prefill_client_company=prefill.client_company,
        prefill_recruiter_name=prefill.recruiter_name,
        prefill_recruiter_email=prefill.recruiter_email,
        prefill_cm_name=prefill.cm_name,
        prefill_cm_email=prefill.cm_email,
        banner=banner,
        suggestion_id=suggestion_id,
    )


async def _handle_recruiter_selected(body: AddonRequest, svc: LoopService, email: str, **kwargs):
    """onChangeAction handler for recruiter directory autocomplete.

    Two callers, distinguished by whether ``draft_id`` is in the action
    parameters:

    - **JIT path** (``draft_id`` present): the coordinator picked a recruiter
      from the autocomplete on a DRAFT_EMAIL card. Commit the contact to the
      loop immediately and refresh the overview so the JIT inputs disappear
      and Send enables. Without this, the onChange would re-render the
      standalone create-loop form by mistake — the bug screenshot showed.

    - **Create-loop form path** (no ``draft_id``): the coordinator picked a
      recruiter inside the standalone create-loop form. Split
      ``"Name <email>"`` into the two fields and re-render the form,
      preserving every other field's typed value via ``prefill_*``.
    """
    request = kwargs.get("request")
    suggestion_id = _get_param(body, "suggestion_id")
    draft_id = _get_param(body, "draft_id")

    def _field(name: str) -> str | None:
        return _get_form_value(body, name)

    if draft_id and suggestion_id:
        # JIT path — stash the pick on draft.pending_jit_data instead of
        # committing to the loop. The actual contact creation + loop attach
        # happens at Send time. This makes misclicks recoverable via the
        # "x" Clear button and matches the user's mental model: nothing is
        # final until you press Send.
        jit_role = _get_param(body, "jit_role") or "recruiter"
        if jit_role == "client_manager":
            name_field = f"jit_cm_name_{suggestion_id}"
            email_field = f"jit_cm_email_{suggestion_id}"
        else:
            name_field = f"jit_recruiter_name_{suggestion_id}"
            email_field = f"jit_recruiter_email_{suggestion_id}"
        raw_name = _field(name_field) or ""
        raw_email = _field(email_field) or ""
        parsed = parse_name_email(raw_name) or parse_name_email(raw_email)
        if parsed is None:
            # Mid-type — no parseable "Name <email>" yet. Just refresh.
            return await _build_refreshed_overview(request, email)
        new_name, new_email = parsed
        new_email = new_email.strip()
        if not new_email:
            return await _build_refreshed_overview(request, email)

        draft_svc = _get_draft_service(request)
        if not draft_svc:
            return await _build_refreshed_overview(request, email)
        draft = await draft_svc.get_draft(draft_id)
        if not draft:
            return await _build_refreshed_overview(request, email)

        new_pending = dict(draft.pending_jit_data or {})
        new_pending[jit_role] = {"name": new_name, "email": new_email}
        await draft_svc.update_pending_jit_data(draft.id, new_pending)
        return await _build_refreshed_overview(request, email)

    # Create-loop form path — preserve the existing behavior.
    raw_name = _field("recruiter_name") or ""
    raw_email = _field("recruiter_email") or ""
    parsed = parse_name_email(raw_name) or parse_name_email(raw_email)
    if parsed is not None:
        new_name, new_email = parsed
    else:
        new_name, new_email = raw_name, raw_email

    return build_create_loop_form(
        gmail_thread_id=_get_param(body, "gmail_thread_id"),
        gmail_subject=_get_param(body, "gmail_subject"),
        gmail_message_id=_get_param(body, "gmail_message_id"),
        prefill_candidate_name=_field("candidate_name"),
        prefill_client_name=_field("client_name"),
        prefill_client_email=_field("client_email"),
        prefill_client_company=_field("client_company"),
        prefill_recruiter_name=new_name or None,
        prefill_recruiter_email=new_email or None,
        prefill_cm_name=_field("cm_name"),
        prefill_cm_email=_field("cm_email"),
        suggestion_id=suggestion_id,
    )


async def _handle_create_loop(body: AddonRequest, svc: LoopService, email: str, **kwargs):
    request = kwargs.get("request")
    suggestion_id = _get_param(body, "suggestion_id")

    # Read form inputs — inline suggestion cards use suffixed names (e.g. candidate_name_{sug_id}),
    # the standalone create form uses unsuffixed names. Try suffixed first.
    def _field(name: str) -> str | None:
        if suggestion_id:
            val = _get_form_value(body, f"{name}_{suggestion_id}")
            if val:
                return val
        return _get_form_value(body, name)

    candidate_name = _field("candidate_name") or "Unknown Candidate"
    client_name = _field("client_name") or ""
    client_email = (_field("client_email") or "").strip()
    client_company = _field("client_company") or ""
    recruiter_name = _field("recruiter_name") or ""
    recruiter_email = (_field("recruiter_email") or "").strip()

    # If the coordinator selected a directory suggestion but clicked Create
    # before onChangeAction fired (or if onChangeAction peer-field updates
    # don't behave as the RFC assumes), either field can still hold
    # "Name <email>" — split it out here defensively.
    parsed = parse_name_email(recruiter_name) or parse_name_email(recruiter_email)
    if parsed is not None:
        recruiter_name, recruiter_email = parsed
        recruiter_email = recruiter_email.strip()

    cm_name = _field("cm_name")
    cm_email = _field("cm_email")

    gmail_thread_id = _get_param(body, "gmail_thread_id")
    gmail_subject = _get_param(body, "gmail_subject")
    gmail_message_id = _get_param(body, "gmail_message_id")

    # Create or find contacts only when the coordinator supplied an email
    # for them. Loops with missing recruiter/client are valid — the JIT
    # widget on the draft card collects them at send time.
    client_contact_id = None
    if client_email:
        client_contact = await svc.find_or_create_client_contact(
            name=client_name or client_email,
            email=client_email,
            company=client_company or None,
        )
        client_contact_id = client_contact.id

    recruiter_id = None
    if recruiter_email:
        # Look up the recruiter's Workspace photo URL only when we're about to
        # create a new contact row. Existing rows keep whatever photo_url they
        # already have (matches the stored-name dedup semantics).
        recruiter_photo_url = await _fetch_recruiter_photo_url(
            request=request,
            svc=svc,
            coordinator_email=email,
            recruiter_email=recruiter_email,
        )
        recruiter = await svc.find_or_create_contact(
            name=recruiter_name or recruiter_email,
            email=recruiter_email,
            role="recruiter",
            photo_url=recruiter_photo_url,
        )
        recruiter_id = recruiter.id

    cm_id = None
    if cm_email:
        cm = await svc.find_or_create_contact(
            name=cm_name or cm_email, email=cm_email, role="client_manager"
        )
        cm_id = cm.id

    title = f"{candidate_name}, {client_company}" if client_company else candidate_name

    await svc.create_loop(
        coordinator_email=email,
        coordinator_name=email.split("@")[0],
        candidate_name=candidate_name,
        client_contact_id=client_contact_id,
        recruiter_id=recruiter_id,
        title=title,
        client_manager_id=cm_id,
        gmail_thread_id=gmail_thread_id,
        gmail_subject=gmail_subject,
    )

    # Resolve the parent suggestion if this was triggered from the overview
    if suggestion_id:
        from api.classifier.service import SuggestionService

        suggestion_svc = SuggestionService(db_pool=svc._pool)
        await suggestion_svc.resolve(suggestion_id, SuggestionStatus.ACCEPTED, email)

    # Enqueue the NextActionAgent — the thread is now linked to the new
    # loop, so the agent will produce follow-up suggestions (e.g.
    # DRAFT_EMAIL to recruiter). Runs async so the UI returns instantly.
    if gmail_thread_id and request:
        redis = get_redis(request)
        if redis:
            try:
                await redis.enqueue_job(
                    "run_next_action_agent",
                    email,
                    gmail_message_id,
                    gmail_thread_id,
                )
                logger.info(
                    "enqueued next action agent for thread %s",
                    gmail_thread_id,
                )
            except Exception:
                logger.exception(
                    "failed to enqueue next action agent for thread %s",
                    gmail_thread_id,
                )
        else:
            logger.warning(
                "redis unavailable — skipping next action agent for thread %s",
                gmail_thread_id,
            )

    # Stay anchored to the just-linked thread. The agent runs async, so
    # this typically renders the placeholder card; the user clicks Refresh
    # (or Gmail re-fires on-message) once the agent has emitted suggestions.
    return await _build_thread_overview_or_placeholder(
        request, email, gmail_thread_id, message_id=gmail_message_id
    )


# ---------------------------------------------------------------------------
# AI Draft action handlers
# ---------------------------------------------------------------------------


def _get_draft_service(request: Request | None):
    """Get DraftService from app state, or None when request is missing."""
    if request is None:
        return None
    return get_draft_service(request)


async def _handle_view_draft(body: AddonRequest, svc: LoopService, email: str, **kwargs):
    """Show a read-only preview of an AI-generated draft."""
    from api.drafts.cards import build_draft_preview

    request = kwargs.get("request")
    draft_svc = _get_draft_service(request)
    if not draft_svc:
        return await _build_refreshed_overview(kwargs.get("request"), email)

    draft_id = _get_param(body, "draft_id")
    if not draft_id:
        return await _build_refreshed_overview(kwargs.get("request"), email)

    draft = await draft_svc.get_draft(draft_id)
    if not draft:
        return await _build_refreshed_overview(kwargs.get("request"), email)

    return build_draft_preview(draft)


async def _apply_jit_contacts(
    *,
    body: AddonRequest,
    request: Request | None,
    svc: LoopService,
    draft_svc,
    draft,
    suggestion_id: str,
    coordinator_email: str,
):
    """Commit pending JIT picks on the draft to the loop, then re-resolve
    recipients. Returns the (possibly updated) draft.

    Reads from ``draft.pending_jit_data`` first (the staged picks from the
    autocomplete dropdown), falling back to form inputs (typed values that
    weren't picked from the dropdown). Clears pending_jit_data after a
    successful commit so the draft is clean if Send is retried.
    """
    pending = dict(draft.pending_jit_data or {})

    def _from_pending_or_form(role: str, name_field: str, email_field: str) -> tuple[str, str]:
        staged = pending.get(role) or {}
        name = staged.get("name") or ""
        email = staged.get("email") or ""
        if not email:
            raw_name = _get_form_value(body, name_field) or ""
            raw_email = _get_form_value(body, email_field) or ""
            parsed = parse_name_email(raw_name) or parse_name_email(raw_email)
            if parsed is not None:
                name, email = parsed
            else:
                name, email = raw_name, raw_email
        return (name.strip(), email.strip())

    recruiter_name, recruiter_email = _from_pending_or_form(
        "recruiter",
        f"jit_recruiter_name_{suggestion_id}",
        f"jit_recruiter_email_{suggestion_id}",
    )
    client_staged = pending.get("client_contact") or {}
    client_name = (
        client_staged.get("name")
        or (_get_form_value(body, f"jit_client_name_{suggestion_id}") or "")
    ).strip()
    client_email = (
        client_staged.get("email")
        or (_get_form_value(body, f"jit_client_email_{suggestion_id}") or "")
    ).strip()
    client_company = (
        client_staged.get("company")
        or (_get_form_value(body, f"jit_client_company_{suggestion_id}") or "")
    ).strip()
    cm_name, cm_email = _from_pending_or_form(
        "client_manager",
        f"jit_cm_name_{suggestion_id}",
        f"jit_cm_email_{suggestion_id}",
    )

    if recruiter_email and draft.loop_id:
        photo_url = await _fetch_recruiter_photo_url(
            request=request,
            svc=svc,
            coordinator_email=coordinator_email,
            recruiter_email=recruiter_email,
        )
        recruiter = await svc.find_or_create_contact(
            name=recruiter_name or recruiter_email,
            email=recruiter_email,
            role="recruiter",
            photo_url=photo_url,
        )
        await svc.set_recruiter(draft.loop_id, recruiter.id, coordinator_email)

    if client_email and draft.loop_id:
        client_contact = await svc.find_or_create_client_contact(
            name=client_name or client_email,
            email=client_email,
            company=client_company or None,
        )
        await svc.set_client_contact(draft.loop_id, client_contact.id, coordinator_email)

    if cm_email and draft.loop_id:
        cm = await svc.find_or_create_contact(
            name=cm_name or cm_email,
            email=cm_email,
            role="client_manager",
        )
        await svc.set_client_manager(draft.loop_id, cm.id, coordinator_email)

    if not recruiter_email and not client_email and not cm_email:
        return draft

    # Re-resolve recipients now that the loop has the contacts attached,
    # and clear the staging area so the draft is clean if Send is retried.
    from api.drafts.service import resolve_recipients

    loop = await svc.get_loop(draft.loop_id)
    to_emails, cc_emails = resolve_recipients(loop, sender_email=coordinator_email)
    await draft_svc.update_draft_recipients(draft.id, to_emails, cc_emails)
    if pending:
        await draft_svc.update_pending_jit_data(draft.id, {})
    return await draft_svc.get_draft(draft.id)


async def _handle_clear_jit(body: AddonRequest, svc: LoopService, email: str, **kwargs):
    """Clear one role from the draft's pending JIT picks.

    Wired to the small "✕ Clear" button next to a staged pick. Only the
    targeted role is removed; other staged picks are preserved.
    """
    request = kwargs.get("request")
    draft_id = _get_param(body, "draft_id")
    role = _get_param(body, "jit_role")
    if not draft_id or not role:
        return await _build_refreshed_overview(request, email)
    draft_svc = _get_draft_service(request)
    if not draft_svc:
        return await _build_refreshed_overview(request, email)
    draft = await draft_svc.get_draft(draft_id)
    if not draft:
        return await _build_refreshed_overview(request, email)

    new_pending = dict(draft.pending_jit_data or {})
    new_pending.pop(role, None)
    await draft_svc.update_pending_jit_data(draft.id, new_pending)
    return await _build_refreshed_overview(request, email)


async def _handle_send_draft(body: AddonRequest, svc: LoopService, email: str, **kwargs):
    """Send an AI-generated draft: update body if edited, send via LoopService, mark sent."""
    from api.classifier.service import SuggestionService

    request = kwargs.get("request")
    draft_svc = _get_draft_service(request)
    if not draft_svc:
        return await _build_refreshed_overview(request, email)

    draft_id = _get_param(body, "draft_id")
    if not draft_id:
        return await _build_refreshed_overview(request, email)

    draft = await draft_svc.get_draft(draft_id)
    if not draft:
        return await _build_refreshed_overview(request, email)

    # If the body was edited inline, use the form value
    # Check both the suggestion-specific input name and the generic "draft_body"
    suggestion_id = _get_param(body, "suggestion_id")
    edited_body = None
    if suggestion_id:
        edited_body = _get_form_value(body, f"draft_body_{suggestion_id}")
    if edited_body is None:
        edited_body = _get_form_value(body, "draft_body")
    send_body = edited_body if edited_body is not None else draft.body
    if edited_body is not None and edited_body != draft.body:
        await draft_svc.update_draft_body(draft.id, send_body)

    # JIT contact collection: when the loop was auto-created with a missing
    # recruiter or client_contact, the draft card renders inline inputs
    # (jit_recruiter_*, jit_client_*). If they're populated, attach the
    # contact to the loop and patch the draft's recipients before sending.
    if suggestion_id and not draft.to_emails:
        draft = await _apply_jit_contacts(
            body=body,
            request=request,
            svc=svc,
            draft_svc=draft_svc,
            draft=draft,
            suggestion_id=suggestion_id,
            coordinator_email=email,
        )
        if not draft.to_emails:
            # Required JIT inputs weren't supplied — bail out. The disabled
            # Send button on the card means we shouldn't normally hit this.
            logger.warning(
                "send_draft: draft %s has no recipients and no JIT inputs supplied",
                draft.id,
            )
            return await _build_refreshed_overview(request, email)

    # Fetch the thread once, up-front. Two downstream blocks consume it:
    #   (1) the RFC 2822 threading headers immediately below, and
    #   (2) build_forwarded_body() for forwards (issue #36).
    # Keeping the fetch separate from its consumers also lets the error policy
    # branch on is_forward: replies soft-fallback (warn + send with no
    # threading headers — the recipient is already on the thread, so Gmail
    # shows them context regardless); forwards raise, because a forward
    # without quoted history is exactly the bug we're fixing.
    thread = None
    gmail = get_gmail(request) if request else None
    if gmail and draft.gmail_thread_id:
        try:
            thread = await gmail.get_thread(email, draft.gmail_thread_id)
        except Exception:
            if draft.is_forward:
                logger.exception(
                    "Failed to fetch thread %s for forward — aborting send",
                    draft.gmail_thread_id,
                )
                raise
            logger.warning(
                "Could not fetch thread %s for reply headers — sending without threading",
                draft.gmail_thread_id,
                exc_info=True,
            )

    # RFC 2822 threading headers so the recipient's Gmail stitches this message
    # into the existing conversation rather than showing it as a new thread.
    #   In-Reply-To: the Message-ID of the message we're directly replying to
    #     (i.e. the last one in the thread).
    #   References: a space-separated chain of every prior Message-ID in the
    #     thread — lets Gmail place this message at the right node in the
    #     thread tree even if some intermediate messages went missing.
    # Without these, Gmail renders the outgoing message as a standalone
    # conversation in the recipient's inbox.
    in_reply_to = None
    references = None
    if thread and thread.messages:
        last_msg = thread.messages[-1]
        in_reply_to = last_msg.message_id_header
        ref_ids = [m.message_id_header for m in thread.messages if m.message_id_header]
        if ref_ids:
            references = " ".join(ref_ids)

    # Forwards: quote the prior thread into the body and prefix "Fwd:" on the
    # subject. Applied only on the wire — the persisted draft body stays short
    # so the sidebar view remains compact.
    send_subject = draft.subject
    if draft.is_forward:
        if not thread or not thread.messages:
            raise RuntimeError(
                f"Cannot forward draft {draft.id}: no thread history available to quote"
            )
        send_body = build_forwarded_body(send_body, thread)
        send_subject = prefix_forward_subject(draft.subject)

    # Send email via existing LoopService path
    await svc.send_email(
        loop_id=draft.loop_id,
        coordinator_email=email,
        to=draft.to_emails,
        subject=send_subject,
        body=send_body,
        cc=draft.cc_emails or None,
        gmail_thread_id=draft.gmail_thread_id,
        in_reply_to=in_reply_to,
        references=references,
    )

    # Mark draft sent + resolve parent suggestion as accepted
    await draft_svc.mark_sent(draft.id)
    suggestion_svc = SuggestionService(db_pool=svc._pool)
    await suggestion_svc.resolve(
        draft.suggestion_id,
        status=SuggestionStatus.ACCEPTED,
        resolved_by=email,
    )

    # Return refreshed overview instead of loop detail
    return await _build_refreshed_overview(request, email)


async def _handle_discard_draft(body: AddonRequest, svc: LoopService, email: str, **kwargs):
    """Discard an AI-generated draft and reject the parent suggestion."""
    from api.classifier.service import SuggestionService

    request = kwargs.get("request")
    draft_svc = _get_draft_service(request)
    if not draft_svc:
        return await _build_refreshed_overview(request, email)

    draft_id = _get_param(body, "draft_id")
    if not draft_id:
        return await _build_refreshed_overview(request, email)

    draft = await draft_svc.get_draft(draft_id)
    if draft:
        await draft_svc.mark_discarded(draft.id)
        suggestion_svc = SuggestionService(db_pool=svc._pool)
        await suggestion_svc.resolve(
            draft.suggestion_id,
            status=SuggestionStatus.REJECTED,
            resolved_by=email,
        )

    return await _build_refreshed_overview(request, email)


async def _handle_accept_suggestion(body: AddonRequest, svc: LoopService, email: str, **kwargs):
    """Generic accept handler — dispatches by suggestion action type."""
    from api.classifier.service import SuggestionService

    request = kwargs.get("request")
    suggestion_id = _get_param(body, "suggestion_id")
    if not suggestion_id or not request:
        return await _build_refreshed_overview(request, email)

    suggestion_svc = SuggestionService(db_pool=svc._pool)
    suggestion = await suggestion_svc.get_suggestion(suggestion_id)
    if not suggestion:
        return await _build_refreshed_overview(request, email)

    # New ADVANCE_STAGE / LINK_THREAD suggestions are auto-resolved by the
    # classifier and never render an Accept button. These branches still
    # fire for post-deploy rows whose resolver Sentry-and-dropped.
    # CREATE_LOOP has its own dedicated submit path (create_loop).
    # DRAFT_EMAIL uses send_draft. ASK_COORDINATOR has no backend yet.
    if suggestion.action == SuggestedAction.ADVANCE_STAGE:
        target_stage = (suggestion.action_data or {}).get("target_stage")
        if suggestion.loop_id and target_stage:
            await svc.advance_state(suggestion.loop_id, StageState(target_stage), email)
        else:
            logger.warning(
                "ADVANCE_STAGE suggestion %s missing loop_id or action_data.target_stage",
                suggestion_id,
            )
    elif suggestion.action == SuggestedAction.LINK_THREAD:
        if suggestion.loop_id and suggestion.gmail_thread_id:
            await svc.link_thread(suggestion.loop_id, suggestion.gmail_thread_id, None, email)
        else:
            logger.warning("LINK_THREAD suggestion %s missing loop_id or thread_id", suggestion_id)
    else:
        logger.warning(
            "accept_suggestion called for unsupported action %s (suggestion %s) — ignoring",
            suggestion.action,
            suggestion_id,
        )
        return await _build_refreshed_overview(request, email)

    # Resolve the suggestion as accepted
    await suggestion_svc.resolve(suggestion_id, SuggestionStatus.ACCEPTED, email)

    return await _build_refreshed_overview(request, email)


async def _handle_reject_suggestion(body: AddonRequest, svc: LoopService, email: str, **kwargs):
    """Dismiss a suggestion — resolve as REJECTED, discard draft if applicable."""
    from api.classifier.service import SuggestionService

    request = kwargs.get("request")
    suggestion_id = _get_param(body, "suggestion_id")
    if not suggestion_id or not request:
        return await _build_refreshed_overview(request, email)

    suggestion_svc = SuggestionService(db_pool=svc._pool)
    suggestion = await suggestion_svc.get_suggestion(suggestion_id)

    # If the suggestion has a draft, discard it too
    if suggestion and suggestion.action == SuggestedAction.DRAFT_EMAIL:
        draft_svc = _get_draft_service(request)
        if draft_svc:
            draft = await draft_svc.get_draft_for_suggestion(suggestion_id)
            if draft:
                await draft_svc.mark_discarded(draft.id)

    await suggestion_svc.resolve(suggestion_id, SuggestionStatus.REJECTED, email)

    return await _build_refreshed_overview(request, email)


async def _handle_show_suggestions_tab(body: AddonRequest, svc: LoopService, email: str, **kwargs):
    """Switch to the Suggestions tab (overview)."""
    request = kwargs.get("request")
    return await _build_refreshed_overview(request, email)


async def _handle_unknown(body: AddonRequest, svc: LoopService, email: str, **kwargs):
    fn = body.common_event_object.invoked_function if body.common_event_object else None
    logger.warning("Unknown invokedFunction: %s", fn)
    return await _build_refreshed_overview(kwargs.get("request"), email)


async def _handle_update_candidate_name(body: AddonRequest, svc: LoopService, email: str, **kwargs):
    """Rename a loop's candidate from the inline overview affordance.

    Surfaced when CREATE_LOOP auto-resolved without a candidate name and
    the loop card shows the placeholder ("Unknown Candidate"). The form
    field is suffixed with the loop_id so it's stable across re-renders.
    """
    request = kwargs.get("request")
    loop_id = _get_param(body, "loop_id")
    if not loop_id:
        return await _build_refreshed_overview(request, email)
    new_name = (_get_form_value(body, f"candidate_name_{loop_id}") or "").strip()
    if not new_name:
        return await _build_refreshed_overview(request, email)

    loop = await svc.get_loop(loop_id)
    if loop and loop.candidate:
        await svc.update_candidate_name(
            candidate_id=loop.candidate.id,
            name=new_name,
            coordinator_email=email,
            loop_id=loop_id,
        )
    return await _build_refreshed_overview(request, email)


_ACTION_HANDLERS = {
    # Loop creation (manual path)
    "show_create_form": _handle_show_create_form,
    "create_loop": _handle_create_loop,
    "recruiter_selected": _handle_recruiter_selected,
    # AI draft actions
    "view_draft": _handle_view_draft,
    "send_draft": _handle_send_draft,
    "discard_draft": _handle_discard_draft,
    # Suggestion actions (ADVANCE_STAGE / LINK_THREAD / ASK_COORDINATOR / dismiss)
    "accept_suggestion": _handle_accept_suggestion,
    "reject_suggestion": _handle_reject_suggestion,
    "show_suggestions_tab": _handle_show_suggestions_tab,
    # JIT candidate rename (when classifier auto-created with placeholder)
    "update_candidate_name": _handle_update_candidate_name,
    # JIT pending-pick management (recruiter / client / CM staged on draft)
    "clear_jit": _handle_clear_jit,
}


# ---------------------------------------------------------------------------
# OAuth flow (no Google add-on token verification — browser-based)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Refresh endpoint (no add-on token — self-closing HTML for overlay polling)
# ---------------------------------------------------------------------------

refresh_router = APIRouter(prefix="/addon", tags=["addon-refresh"])


@refresh_router.get("/refresh")
async def addon_refresh() -> HTMLResponse:
    """Return a self-closing HTML page for the OpenLink overlay refresh mechanism.

    When the sidebar's "Refresh" button is clicked, Google opens this URL in
    a small overlay. The page auto-closes, triggering the onClose: RELOAD
    behavior which re-fires the add-on's homepage/contextual trigger.
    """
    return HTMLResponse(
        "<html><body>"
        "<script>setTimeout(function(){window.close()},100);</script>"
        "Refreshing..."
        "</body></html>"
    )


oauth_router = APIRouter(prefix="/addon/oauth", tags=["oauth"])


@oauth_router.get("/start")
async def oauth_start(user_email: str, request: Request):
    """Redirect the coordinator to Google's OAuth consent screen."""
    from urllib.parse import urlencode

    from api.gmail.auth import SCOPES

    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    base = str(request.url).split("/addon/oauth/")[0]
    redirect_uri = f"{base}/addon/oauth/callback"

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "login_hint": user_email,
        "state": user_email,
    }
    auth_url = f"https://accounts.google.com/o/oauth2/auth?{urlencode(params)}"
    return RedirectResponse(auth_url)


@oauth_router.get("/callback")
async def oauth_callback(code: str, state: str, request: Request):
    """Handle the OAuth callback — exchange code for tokens and store."""
    import requests as http_requests

    from api.gmail.auth import SCOPES

    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    base = str(request.url).split("/addon/oauth/")[0]
    redirect_uri = f"{base}/addon/oauth/callback"
    user_email = state

    # Exchange authorization code for tokens directly (no PKCE)
    token_resp = http_requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=10,
    )
    token_data = token_resp.json()

    if "error" in token_data:
        logger.error("Token exchange failed: %s", token_data)
        return HTMLResponse(
            f"<html><body><h2>Authorization failed</h2>"
            f"<p>{token_data.get('error_description', token_data['error'])}</p>"
            f"</body></html>",
            status_code=400,
        )

    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        logger.error("No refresh token in response: %s", token_data)
        return HTMLResponse(
            "<html><body><h2>Authorization failed</h2>"
            "<p>No refresh token received. Try revoking access at "
            "myaccount.google.com/permissions and re-authorizing.</p>"
            "</body></html>",
            status_code=400,
        )

    # Store the refresh token
    gmail = get_gmail(request)
    if gmail is None:
        return HTMLResponse(
            "<html><body>Server misconfigured: Gmail not available.</body></html>", status_code=503
        )
    await gmail._token_store.store_token(
        user_email=user_email,
        refresh_token=refresh_token,
        scopes=SCOPES,
    )
    logger.info("Stored OAuth token for %s", user_email)

    # Establish Gmail history baseline immediately so no messages are missed.
    # Any email arriving after this point will have a historyId > baseline.
    try:
        profile = await gmail.get_profile(user_email)
        history_id = str(profile["historyId"])
        await gmail._token_store.update_history_id(user_email, history_id)
        logger.info("Established baseline for %s at history_id=%s", user_email, history_id)
    except Exception:
        logger.exception("Failed to establish baseline for %s during OAuth", user_email)

    return HTMLResponse(
        "<html><body><h2>Gmail access authorized.</h2>"
        "<p>You can close this tab and return to Gmail.</p>"
        "</body></html>"
    )
