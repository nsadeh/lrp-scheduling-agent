"""Google Workspace Add-on HTTP endpoints.

Google POSTs to these routes when a coordinator interacts with the Gmail sidebar.
Token verification is applied at the router level via Depends().
"""

from __future__ import annotations

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
    SuggestionsActionResponse,
    SuggestionsResponse,
    UpdateCard,
)
from api.classifier.models import SuggestedAction, SuggestionStatus
from api.gmail.exceptions import GmailScopeError, GmailValidationError
from api.overview.cards import build_overview
from api.overview.service import OverviewService
from api.scheduling.cards import (
    build_auth_required,
    build_contextual_unlinked,
    build_create_loop_form,
    build_error_card,
)
from api.scheduling.models import StageState
from api.scheduling.service import LoopService  # noqa: TC001

logger = logging.getLogger(__name__)

addon_router = APIRouter(
    prefix="/addon",
    tags=["addon"],
)


def _get_scheduling(request: Request) -> LoopService:
    return request.app.state.scheduling


def _get_overview_service(request: Request) -> OverviewService:
    svc = getattr(request.app.state, "overview_service", None)
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

    Tries userIdToken first. Falls back to userOAuthToken or systemIdToken
    if available. All are Google-signed JWTs with an email claim.

    Raises ValueError if the email cannot be determined.
    """
    import base64
    import json

    auth = body.authorization_event_object
    if not auth:
        logger.error("No authorizationEventObject in request body")
        raise ValueError("No authorizationEventObject in add-on request")

    # Try each token type — they're all JWTs with potential email claims
    token_sources = [
        ("userIdToken", auth.user_id_token),
        ("systemIdToken", auth.system_id_token),
    ]

    # Log what we received for debugging
    available = [name for name, val in token_sources if val]
    logger.info("Available tokens in request: %s", available)

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


_GMAIL_CONTEXTUAL_ID_RE = re.compile(r"^(?:thread-f|msg-f):(\d+)$")

# Format used in autocomplete dropdown items and parsed back out on selection.
# Matching is anchored and tolerates spaces around the angle brackets.
_NAME_EMAIL_RE = re.compile(r"^\s*(.+?)\s*<\s*([^<>\s]+@[^<>\s]+)\s*>\s*$")


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


def _normalize_gmail_id(raw_id: str | None) -> str | None:
    """Convert Google contextual-trigger IDs to Gmail API hex format.

    Google's contextual triggers send IDs like 'thread-f:1862729221917227576'
    (decimal with prefix), but the Gmail API and our database use the hex
    form '19d9be5bb11dba38'. They're the same number in different bases.
    """
    if not raw_id:
        return None
    m = _GMAIL_CONTEXTUAL_ID_RE.match(raw_id)
    if m:
        return hex(int(m.group(1)))[2:]  # strip '0x' prefix
    return raw_id


async def _check_gmail_auth(request: Request, user_email: str) -> CardResponse | None:
    """Check if the coordinator has stored Gmail credentials.

    Returns an auth-required card if not, or None if authorized.
    """
    gmail = getattr(request.app.state, "gmail", None)
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
            "Homepage auth keys: %s, userIdToken: %s, userOAuthToken: %s",
            list(auth_dump.keys()),
            has_id,
            has_oauth,
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
    if body.gmail:
        thread_id = _normalize_gmail_id(body.gmail.thread_id)
        message_id = _normalize_gmail_id(body.gmail.message_id)

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
        svc = _get_scheduling(request)
        loop = await svc.find_loop_by_thread(thread_id)
        logger.info(
            "on-message: thread_id=%s, linked_loop=%s",
            thread_id,
            loop.id if loop else None,
        )
        if loop:
            # Thread is linked but no pending suggestions — show full overview
            card = await _build_refreshed_overview(request, email)
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

    svc = _get_scheduling(request)
    email = _get_user_email(body)

    # Read action name from parameters (set by our card builders)
    fn = _get_param(body, "action_name")
    # Fallback to invokedFunction for backward compatibility
    if not fn and body.common_event_object:
        fn = body.common_event_object.invoked_function

    handler = _ACTION_HANDLERS.get(fn or "", _handle_unknown)
    try:
        card = await handler(body, svc, email, request=request)
    except GmailScopeError as exc:
        logger.warning("Gmail scope error in action %s: %s", fn, exc)
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

    Wired from ``TextInput.autoCompleteAction`` — Google POSTs here per
    keystroke (after its own debounce) with the current input value as the
    ``query`` parameter. Returns a flat ``SuggestionsResponse`` with
    ``"Name <email>"`` strings drawn from the calling coordinator's
    Workspace directory via the People API.
    """
    email = _get_user_email(body)
    query = (_get_param(body, "query") or "").strip()
    if not query:
        return _empty_suggestions().model_dump(by_alias=True, exclude_none=True)

    gmail = getattr(request.app.state, "gmail", None)
    if gmail is None:
        logger.warning("directory/search: no gmail client configured")
        return _empty_suggestions().model_dump(by_alias=True, exclude_none=True)

    try:
        creds = await gmail._token_store.load_credentials(email)
    except GmailScopeError:
        # Suggestions can't surface the auth card; the show_create_form path
        # below pre-checks scopes so this branch is only hit if a coordinator
        # opens the form before the wrapper noticed missing scopes (race).
        logger.info("directory/search: scope error for %s, returning empty", email)
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
    response = SuggestionsResponse(
        action=SuggestionsActionResponse(suggestions=Suggestions(items=items))
    )
    return response.model_dump(by_alias=True, exclude_none=True)


def _empty_suggestions() -> SuggestionsResponse:
    return SuggestionsResponse(action=SuggestionsActionResponse(suggestions=Suggestions(items=[])))


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
    gmail = getattr(request.app.state, "gmail", None)
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


async def _handle_show_create_form(body: AddonRequest, svc: LoopService, email: str, **kwargs):
    request = kwargs.get("request")
    # Pre-check coordinator's directory.readonly scope here, BEFORE the
    # autocomplete callbacks fire on the rendered form. autoCompleteAction
    # responses can't surface the auth-required card, so we have to bounce
    # to re-consent at form-entry time. The action wrapper catches
    # GmailScopeError and shows build_auth_required for us.
    gmail = getattr(request.app.state, "gmail", None) if request else None
    if gmail is not None:
        try:
            await gmail._token_store.load_credentials(email)
        except GmailScopeError:
            raise

    gmail_thread_id = _get_param(body, "gmail_thread_id")
    message_id = _get_param(body, "message_id")

    # Check for prefill params from suggestion entities (passed by overview card)
    prefill_candidate_name = _get_param(body, "prefill_candidate_name")
    prefill_client_name = _get_param(body, "prefill_client_name")
    prefill_client_email = _get_param(body, "prefill_client_email")
    prefill_client_company = _get_param(body, "prefill_client_company")
    prefill_recruiter_name = _get_param(body, "prefill_recruiter_name")
    prefill_recruiter_email = _get_param(body, "prefill_recruiter_email")
    prefill_cm_name = _get_param(body, "prefill_cm_name")
    prefill_cm_email = _get_param(body, "prefill_cm_email")
    gmail_subject = None

    # If no prefill from suggestion, try Gmail message metadata
    if not prefill_client_email and message_id and svc._gmail:
        try:
            msg = await svc._gmail.get_message(email, message_id)
            if msg.from_:
                prefill_client_name = prefill_client_name or msg.from_.name or ""
                prefill_client_email = msg.from_.email
            if msg.cc:
                prefill_cm_name = prefill_cm_name or msg.cc[0].name or ""
                prefill_cm_email = prefill_cm_email or msg.cc[0].email
            gmail_subject = msg.subject
        except Exception:
            logger.warning("Could not fetch message %s for pre-fill", message_id, exc_info=True)

    # If a pre-filled email matches an existing contact, prefer the stored
    # name/company so the form shows what will actually be persisted on
    # submit — the dedup logic in find_or_create_contact keeps the stored
    # row untouched, so showing the classifier-suggested name here would
    # mislead the coordinator.
    if prefill_client_email:
        existing_client = await svc.get_client_contact_by_email(prefill_client_email)
        if existing_client is not None:
            prefill_client_name = existing_client.name
            if existing_client.company:
                prefill_client_company = existing_client.company
    if prefill_recruiter_email:
        existing_recruiter = await svc.get_contact_by_email(
            prefill_recruiter_email, role="recruiter"
        )
        if existing_recruiter is not None:
            prefill_recruiter_name = existing_recruiter.name
    if prefill_cm_email:
        existing_cm = await svc.get_contact_by_email(prefill_cm_email, role="client_manager")
        if existing_cm is not None:
            prefill_cm_name = existing_cm.name

    # Pass suggestion_id through so create_loop can resolve it
    suggestion_id = _get_param(body, "suggestion_id")

    return build_create_loop_form(
        gmail_thread_id=gmail_thread_id,
        gmail_subject=gmail_subject,
        gmail_message_id=message_id,
        prefill_candidate_name=prefill_candidate_name,
        prefill_client_name=prefill_client_name,
        prefill_client_email=prefill_client_email,
        prefill_client_company=prefill_client_company,
        prefill_recruiter_name=prefill_recruiter_name,
        prefill_recruiter_email=prefill_recruiter_email,
        prefill_cm_name=prefill_cm_name,
        prefill_cm_email=prefill_cm_email,
        suggestion_id=suggestion_id,
    )


async def _handle_recruiter_selected(body: AddonRequest, svc: LoopService, email: str, **kwargs):
    """onChangeAction handler — split ``"Name <email>"`` into name+email fields.

    Fired when either recruiter field changes in the STANDALONE create-loop
    form. If the value matches our "Display Name <email@domain>" sentinel
    (what the directory suggestion dropdown emits), split it into
    ``recruiter_name`` and ``recruiter_email``. Otherwise leave both fields
    as-is. Always preserves the other form fields — the UpdateCard re-renders
    the full form, so every input value round-trips through prefill_* kwargs.

    For the inline form inside overview suggestion cards this handler is not
    wired: that card can't be re-rendered in isolation. Inline callers rely
    on the defensive parse in ``_handle_create_loop`` instead.
    """
    suggestion_id = _get_param(body, "suggestion_id")

    def _field(name: str) -> str | None:
        return _get_form_value(body, name)

    raw_name = _field("recruiter_name") or ""
    raw_email = _field("recruiter_email") or ""

    # Either field may carry the "Name <email>" payload (the coordinator
    # could have typed in either one and picked from its autocomplete).
    parsed = parse_name_email(raw_name) or parse_name_email(raw_email)
    if parsed is not None:
        new_name, new_email = parsed
    else:
        # Not a directory selection — leave fields as the coordinator typed.
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
        prefill_first_stage=_field("first_stage_name"),
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

    candidate_name = _field("candidate_name") or "Unknown"
    client_name = _field("client_name") or "Unknown"
    client_email = (_field("client_email") or "").strip()
    client_company = _field("client_company") or ""
    recruiter_name = _field("recruiter_name") or "Unknown"
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
    first_stage = _field("first_stage_name") or "Round 1"

    if not client_email or not recruiter_email:
        missing = []
        if not client_email:
            missing.append("Client Email")
        if not recruiter_email:
            missing.append("Recruiter Email")
        return build_create_loop_form(
            gmail_thread_id=_get_param(body, "gmail_thread_id"),
            gmail_subject=_get_param(body, "gmail_subject"),
            gmail_message_id=_get_param(body, "gmail_message_id"),
            prefill_candidate_name=candidate_name if candidate_name != "Unknown" else None,
            prefill_client_name=client_name if client_name != "Unknown" else None,
            prefill_client_email=client_email or None,
            prefill_client_company=client_company or None,
            prefill_recruiter_name=recruiter_name if recruiter_name != "Unknown" else None,
            prefill_recruiter_email=recruiter_email or None,
            prefill_cm_name=cm_name,
            prefill_cm_email=cm_email,
            prefill_first_stage=first_stage,
            error_message=f"Required: {', '.join(missing)}",
            suggestion_id=suggestion_id,
        )
    gmail_thread_id = _get_param(body, "gmail_thread_id")
    gmail_subject = _get_param(body, "gmail_subject")

    # Create or find contacts
    client_contact = await svc.find_or_create_client_contact(
        name=client_name, email=client_email, company=client_company
    )

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
        name=recruiter_name,
        email=recruiter_email,
        role="recruiter",
        photo_url=recruiter_photo_url,
    )
    cm_id = None
    if cm_name and cm_email:
        cm = await svc.find_or_create_contact(name=cm_name, email=cm_email, role="client_manager")
        cm_id = cm.id

    title = f"{candidate_name}, {client_company}"

    await svc.create_loop(
        coordinator_email=email,
        coordinator_name=email.split("@")[0],
        candidate_name=candidate_name,
        client_contact_id=client_contact.id,
        recruiter_id=recruiter.id,
        title=title,
        first_stage_name=first_stage,
        client_manager_id=cm_id,
        gmail_thread_id=gmail_thread_id,
        gmail_subject=gmail_subject,
    )

    # Resolve the parent suggestion if this was triggered from the overview
    if suggestion_id:
        from api.classifier.service import SuggestionService

        suggestion_svc = SuggestionService(db_pool=svc._pool)
        await suggestion_svc.resolve(suggestion_id, SuggestionStatus.ACCEPTED, email)

    # Enqueue background reclassification — the thread is now linked to the
    # new loop, so the classifier will produce follow-up suggestions
    # (e.g. DRAFT_EMAIL to recruiter). Runs async so the UI returns instantly.
    gmail_message_id = _get_param(body, "gmail_message_id")
    if gmail_thread_id and request:
        redis = getattr(request.app.state, "redis", None)
        if redis:
            try:
                await redis.enqueue_job(
                    "reclassify_after_loop_creation",
                    email,
                    gmail_message_id,
                    gmail_thread_id,
                )
                logger.info(
                    "enqueued background reclassification for thread %s",
                    gmail_thread_id,
                )
            except Exception:
                logger.exception(
                    "failed to enqueue reclassification for thread %s",
                    gmail_thread_id,
                )
        else:
            logger.warning(
                "redis unavailable — skipping background reclassification for thread %s",
                gmail_thread_id,
            )

    return await _build_refreshed_overview(request, email)


# ---------------------------------------------------------------------------
# AI Draft action handlers
# ---------------------------------------------------------------------------


def _get_draft_service(request: Request | None):
    """Get DraftService from app state, or None if not available."""
    if request is None:
        return None
    return getattr(request.app.state, "draft_service", None)


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

    # Resolve threading headers so the recipient sees this in the same thread.
    # In-Reply-To + References are RFC 2822 headers that tell the recipient's
    # email client to display this as a reply (or forward) in the thread.
    in_reply_to = None
    references = None
    gmail = getattr(request.app.state, "gmail", None) if request else None
    if gmail and draft.gmail_thread_id:
        try:
            thread = await gmail.get_thread(email, draft.gmail_thread_id)
            if thread.messages:
                last_msg = thread.messages[-1]
                in_reply_to = last_msg.message_id_header
                # Build References chain from all messages in thread
                ref_ids = [m.message_id_header for m in thread.messages if m.message_id_header]
                if ref_ids:
                    references = " ".join(ref_ids)
        except Exception:
            logger.warning(
                "Could not fetch thread %s for reply headers — sending without threading",
                draft.gmail_thread_id,
                exc_info=True,
            )

    # Send email via existing LoopService path
    await svc.send_email(
        loop_id=draft.loop_id,
        stage_id=draft.stage_id,
        coordinator_email=email,
        to=draft.to_emails,
        subject=draft.subject,
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

    # Dispatch by action type — only handle actions that can be one-click accepted.
    # CREATE_LOOP and DRAFT_EMAIL have their own dedicated flows (show_create_form, send_draft).
    # ASK_COORDINATOR has no backend action yet.
    if suggestion.action == SuggestedAction.ADVANCE_STAGE:
        if suggestion.stage_id and suggestion.target_state:
            await svc.advance_stage(suggestion.stage_id, StageState(suggestion.target_state), email)
        else:
            logger.warning(
                "ADVANCE_STAGE suggestion %s missing stage_id or target_state", suggestion_id
            )
    elif suggestion.action == SuggestedAction.MARK_COLD:
        if suggestion.stage_id:
            await svc.mark_cold(suggestion.stage_id, email)
        else:
            logger.warning("MARK_COLD suggestion %s missing stage_id", suggestion_id)
    elif suggestion.action == SuggestedAction.LINK_THREAD:
        target_loop_id = suggestion.loop_id or suggestion.extracted_entities.get("target_loop_id")
        if target_loop_id and suggestion.gmail_thread_id:
            await svc.link_thread(target_loop_id, suggestion.gmail_thread_id, None, email)
        else:
            logger.warning("LINK_THREAD suggestion %s missing loop_id or thread_id", suggestion_id)
    else:
        # CREATE_LOOP, DRAFT_EMAIL, ASK_COORDINATOR, NO_ACTION — should not reach here
        # via normal UI. Don't silently mark as accepted.
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


_ACTION_HANDLERS = {
    # Loop creation (used by CREATE_LOOP suggestion)
    "show_create_form": _handle_show_create_form,
    "create_loop": _handle_create_loop,
    "recruiter_selected": _handle_recruiter_selected,
    # AI draft actions
    "view_draft": _handle_view_draft,
    "send_draft": _handle_send_draft,
    "discard_draft": _handle_discard_draft,
    # Suggestion actions (new suggestion-centric UI)
    "accept_suggestion": _handle_accept_suggestion,
    "reject_suggestion": _handle_reject_suggestion,
    "show_suggestions_tab": _handle_show_suggestions_tab,
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
    gmail = request.app.state.gmail
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
