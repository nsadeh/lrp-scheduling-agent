"""Google Workspace Add-on HTTP endpoints.

Google POSTs to these routes when a coordinator interacts with the Gmail sidebar.
Token verification is applied at the router level via Depends().
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from api.addon.models import (
    ActionResponse,
    AddonRequest,
    CardResponse,
    PushCard,
    UpdateCard,
)
from api.classifier.models import SuggestedAction, SuggestionStatus
from api.gmail.exceptions import GmailScopeError, GmailValidationError
from api.overview.cards import build_overview
from api.overview.service import OverviewService
from api.scheduling.cards import (
    build_add_time_slot_form,
    build_auth_required,
    build_compose_email,
    build_contextual_unlinked,
    build_create_loop_form,
    build_drafts_tab,
    build_error_card,
    build_loop_detail,
    build_revive_form,
    build_status_board,
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

    auth_card = await _check_gmail_auth(request, email)
    if auth_card:
        return _as_push(auth_card).model_dump(by_alias=True, exclude_none=True)

    thread_id = None
    message_id = None
    if body.gmail:
        thread_id = body.gmail.thread_id
        message_id = body.gmail.message_id

    if not thread_id:
        # No thread context — show full overview
        card = await _build_refreshed_overview(request, email)
        return _as_push(card).model_dump(by_alias=True, exclude_none=True)

    # Show suggestions filtered to this thread
    overview_svc = _get_overview_service(request)
    base_url = _get_base_url(request)
    groups = await overview_svc.get_thread_overview_data(thread_id, email)

    if groups:
        card = build_overview(groups, base_url=base_url)
    else:
        # No suggestions for this thread — check if it's linked to a loop
        svc = _get_scheduling(request)
        loop = await svc.find_loop_by_thread(thread_id)
        if loop:
            card = build_loop_detail(loop)
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


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


async def _handle_show_create_form(body: AddonRequest, svc: LoopService, email: str, **_):
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

    # Pass suggestion_id through so create_loop can resolve it
    suggestion_id = _get_param(body, "suggestion_id")

    return build_create_loop_form(
        gmail_thread_id=gmail_thread_id,
        gmail_subject=gmail_subject,
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


async def _handle_create_loop(body: AddonRequest, svc: LoopService, email: str, **_):
    candidate_name = _get_form_value(body, "candidate_name") or "Unknown"
    client_name = _get_form_value(body, "client_name") or "Unknown"
    client_email = (_get_form_value(body, "client_email") or "").strip()
    client_company = _get_form_value(body, "client_company") or ""
    recruiter_name = _get_form_value(body, "recruiter_name") or "Unknown"
    recruiter_email = (_get_form_value(body, "recruiter_email") or "").strip()

    cm_name = _get_form_value(body, "cm_name")
    cm_email = _get_form_value(body, "cm_email")
    first_stage = _get_form_value(body, "first_stage_name") or "Round 1"

    if not client_email or not recruiter_email:
        missing = []
        if not client_email:
            missing.append("Client Email")
        if not recruiter_email:
            missing.append("Recruiter Email")
        return build_create_loop_form(
            gmail_thread_id=_get_param(body, "gmail_thread_id"),
            gmail_subject=_get_param(body, "gmail_subject"),
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
        )
    gmail_thread_id = _get_param(body, "gmail_thread_id")
    gmail_subject = _get_param(body, "gmail_subject")

    # Create or find contacts
    client_contact = await svc.find_or_create_client_contact(
        name=client_name, email=client_email, company=client_company
    )
    recruiter = await svc.find_or_create_contact(
        name=recruiter_name, email=recruiter_email, role="recruiter"
    )
    cm_id = None
    if cm_name and cm_email:
        cm = await svc.find_or_create_contact(name=cm_name, email=cm_email, role="client_manager")
        cm_id = cm.id

    title = f"{candidate_name}, {client_company}"

    loop = await svc.create_loop(
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
    suggestion_id = _get_param(body, "suggestion_id")
    if suggestion_id:
        from api.classifier.service import SuggestionService

        suggestion_svc = SuggestionService(db_pool=svc._pool)
        await suggestion_svc.resolve(suggestion_id, SuggestionStatus.ACCEPTED, email)

    return build_loop_detail(loop)


async def _handle_view_loop(body: AddonRequest, svc: LoopService, email: str, **_):
    loop_id = _get_param(body, "loop_id")
    if not loop_id:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)
    loop = await svc.get_loop(loop_id)
    return build_loop_detail(loop)


async def _handle_advance_stage(body: AddonRequest, svc: LoopService, email: str, **_):
    stage_id = _get_param(body, "stage_id")
    to_state = _get_param(body, "to_state")
    if not stage_id or not to_state:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    stage = await svc.advance_stage(stage_id, StageState(to_state), email)
    loop = await svc.get_loop(stage.loop_id)
    return build_loop_detail(loop)


async def _handle_mark_cold(body: AddonRequest, svc: LoopService, email: str, **_):
    stage_id = _get_param(body, "stage_id")
    if not stage_id:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    stage = await svc.mark_cold(stage_id, email)
    loop = await svc.get_loop(stage.loop_id)
    return build_loop_detail(loop)


async def _handle_show_revive(body: AddonRequest, svc: LoopService, email: str, **_):
    stage_id = _get_param(body, "stage_id")
    loop_id = _get_param(body, "loop_id") or ""
    if not stage_id:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    async with svc._pool.connection() as conn:
        from api.scheduling.queries import queries

        row = await queries.get_stage(conn, id=stage_id)
        if row is None:
            board = await svc.get_status_board(email)
            return build_drafts_tab(board)
        from api.scheduling.service import _row_to_stage

        stage = _row_to_stage(row)
    return build_revive_form(stage, loop_id)


async def _handle_revive_stage(body: AddonRequest, svc: LoopService, email: str, **_):
    stage_id = _get_param(body, "stage_id")
    to_state = _get_form_value(body, "revive_to_state")
    if not stage_id or not to_state:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    stage = await svc.revive_stage(stage_id, StageState(to_state), email)
    loop = await svc.get_loop(stage.loop_id)
    return build_loop_detail(loop)


async def _handle_add_stage(body: AddonRequest, svc: LoopService, email: str, **_):
    loop_id = _get_param(body, "loop_id")
    if not loop_id:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    # Determine next stage name
    loop = await svc.get_loop(loop_id)
    next_num = len(loop.stages) + 1
    stage_name = f"Round {next_num}"

    await svc.add_stage(loop_id, stage_name, email)
    loop = await svc.get_loop(loop_id)
    return build_loop_detail(loop)


async def _handle_compose_email(body: AddonRequest, svc: LoopService, email: str, **_):
    stage_id = _get_param(body, "stage_id")
    loop_id = _get_param(body, "loop_id")
    if not stage_id or not loop_id:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    loop = await svc.get_loop(loop_id)
    stage = next((s for s in loop.stages if s.id == stage_id), None)
    if not stage:
        return build_loop_detail(loop)

    # Use centralized recipient routing (single source of truth)
    from api.drafts.service import resolve_recipients

    to_emails, _ = resolve_recipients(loop, stage)
    to_email = to_emails[0] if to_emails else ""
    subject = f"Re: {loop.title}"

    gmail_thread_id = loop.email_threads[0].gmail_thread_id if loop.email_threads else None
    return build_compose_email(loop, stage, to_email, subject, gmail_thread_id)


async def _handle_send_email(body: AddonRequest, svc: LoopService, email: str, **_):
    stage_id = _get_param(body, "stage_id")
    loop_id = _get_param(body, "loop_id")
    to_email = _get_param(body, "to_email")
    subject = _get_param(body, "subject")
    email_body = _get_form_value(body, "email_body") or ""
    gmail_thread_id = _get_param(body, "gmail_thread_id")

    if not stage_id or not loop_id or not to_email:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    # Determine auto-advance based on current stage state
    loop = await svc.get_loop(loop_id)
    stage = next((s for s in loop.stages if s.id == stage_id), None)
    auto_advance_to = None
    if stage:
        if stage.state == StageState.NEW:
            auto_advance_to = StageState.AWAITING_CANDIDATE
        elif stage.state == StageState.AWAITING_CANDIDATE:
            auto_advance_to = StageState.AWAITING_CLIENT

    await svc.send_email(
        loop_id=loop_id,
        stage_id=stage_id,
        coordinator_email=email,
        to=[to_email],
        subject=subject or "",
        body=email_body,
        gmail_thread_id=gmail_thread_id,
        auto_advance_to=auto_advance_to,
    )

    loop = await svc.get_loop(loop_id)
    return build_loop_detail(loop)


async def _handle_link_thread(body: AddonRequest, svc: LoopService, email: str, **_):
    loop_id = _get_param(body, "loop_id")
    gmail_thread_id = body.gmail.thread_id if body.gmail else None
    if not loop_id or not gmail_thread_id:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    await svc.link_thread(loop_id, gmail_thread_id, None, email)
    loop = await svc.get_loop(loop_id)
    return build_loop_detail(loop)


async def _handle_show_add_time_slot(body: AddonRequest, svc: LoopService, email: str, **_):
    stage_id = _get_param(body, "stage_id")
    loop_id = _get_param(body, "loop_id") or ""
    if not stage_id:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    async with svc._pool.connection() as conn:
        from api.scheduling.queries import queries

        row = await queries.get_stage(conn, id=stage_id)
        if row is None:
            board = await svc.get_status_board(email)
            return build_drafts_tab(board)
        from api.scheduling.service import _row_to_stage

        stage = _row_to_stage(row)
    return build_add_time_slot_form(stage, loop_id)


async def _handle_save_time_slot(body: AddonRequest, svc: LoopService, email: str, **_):
    stage_id = _get_param(body, "stage_id")
    loop_id = _get_param(body, "loop_id")
    if not stage_id or not loop_id:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    date_str = _get_form_value(body, "date") or ""
    time_str = _get_form_value(body, "time") or ""
    tz_str = _get_form_value(body, "timezone") or "America/New_York"
    duration = int(_get_form_value(body, "duration") or "60")
    zoom_link = _get_form_value(body, "zoom_link")

    try:
        tz = ZoneInfo(tz_str)
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    except (ValueError, KeyError):
        # Bad input — go back to the loop detail
        loop = await svc.get_loop(loop_id)
        return build_loop_detail(loop)

    await svc.add_time_slot(
        stage_id=stage_id,
        start_time=dt,
        duration_minutes=duration,
        timezone=tz_str,
        coordinator_email=email,
        zoom_link=zoom_link,
    )
    loop = await svc.get_loop(loop_id)
    return build_loop_detail(loop)


async def _handle_show_drafts_tab(body: AddonRequest, svc: LoopService, email: str, **kwargs):
    board = await svc.get_status_board(email)
    card = build_drafts_tab(board)

    # Inject pending AI drafts at the top if DraftService is available
    request = kwargs.get("request")
    draft_svc = _get_draft_service(request) if request else None
    if draft_svc:
        from api.drafts.cards import build_drafts_list_sections

        pending_drafts = await draft_svc.get_pending_drafts(email)
        if pending_drafts:
            # Insert AI draft sections after the tab buttons (first section)
            draft_sections = build_drafts_list_sections(pending_drafts)
            nav = card.action.navigations[0]
            existing_sections = nav.update_card.sections
            nav.update_card.sections = (
                existing_sections[:1] + draft_sections + existing_sections[1:]
            )

    return card


async def _handle_show_status_tab(body: AddonRequest, svc: LoopService, email: str, **_):
    board = await svc.get_status_board(email)
    return build_status_board(board)


async def _handle_forward_thread(body: AddonRequest, svc: LoopService, email: str, **_):
    """Forward the current thread to the recruiter (no body) and advance to AWAITING_CANDIDATE."""
    stage_id = _get_param(body, "stage_id")
    loop_id = _get_param(body, "loop_id")
    if not stage_id or not loop_id:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    loop = await svc.get_loop(loop_id)
    if not loop.recruiter or not loop.recruiter.email:
        return build_loop_detail(loop)

    gmail_thread_id = loop.email_threads[0].gmail_thread_id if loop.email_threads else None
    subject = f"Re: {loop.title}"

    await svc.send_email(
        loop_id=loop_id,
        stage_id=stage_id,
        coordinator_email=email,
        to=[loop.recruiter.email],
        subject=subject,
        body="",
        gmail_thread_id=gmail_thread_id,
        auto_advance_to=StageState.AWAITING_CANDIDATE,
    )

    loop = await svc.get_loop(loop_id)
    return build_loop_detail(loop)


async def _handle_send_inline_email(body: AddonRequest, svc: LoopService, email: str, **_):
    """Send email to client with inline-composed body, advance to AWAITING_CLIENT."""
    stage_id = _get_param(body, "stage_id")
    loop_id = _get_param(body, "loop_id")
    if not stage_id or not loop_id:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    loop = await svc.get_loop(loop_id)
    if not loop.client_contact or not loop.client_contact.email:
        return build_loop_detail(loop)

    email_body = _get_form_value(body, "email_body") or ""
    gmail_thread_id = loop.email_threads[0].gmail_thread_id if loop.email_threads else None
    subject = f"Re: {loop.title} - Candidate Availability"

    await svc.send_email(
        loop_id=loop_id,
        stage_id=stage_id,
        coordinator_email=email,
        to=[loop.client_contact.email],
        subject=subject,
        body=email_body,
        gmail_thread_id=gmail_thread_id,
        auto_advance_to=StageState.AWAITING_CLIENT,
    )

    loop = await svc.get_loop(loop_id)
    return build_loop_detail(loop)


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
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    draft_id = _get_param(body, "draft_id")
    if not draft_id:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    draft = await draft_svc.get_draft(draft_id)
    if not draft:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    return build_draft_preview(draft)


async def _handle_edit_draft(body: AddonRequest, svc: LoopService, email: str, **kwargs):
    """Show the editable draft form with TextInput for body."""
    from api.drafts.cards import build_draft_edit

    request = kwargs.get("request")
    draft_svc = _get_draft_service(request)
    if not draft_svc:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    draft_id = _get_param(body, "draft_id")
    if not draft_id:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    draft = await draft_svc.get_draft(draft_id)
    if not draft:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    return build_draft_edit(draft)


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

    # Determine auto-advance based on stage state
    loop = await svc.get_loop(draft.loop_id)
    stage = next((s for s in loop.stages if s.id == draft.stage_id), None)
    auto_advance_to = None
    if stage:
        if stage.state == StageState.NEW:
            auto_advance_to = StageState.AWAITING_CANDIDATE
        elif stage.state == StageState.AWAITING_CANDIDATE:
            auto_advance_to = StageState.AWAITING_CLIENT

    # Send email via existing LoopService path
    await svc.send_email(
        loop_id=draft.loop_id,
        stage_id=draft.stage_id,
        coordinator_email=email,
        to=draft.to_emails,
        subject=draft.subject,
        body=send_body,
        gmail_thread_id=draft.gmail_thread_id,
        auto_advance_to=auto_advance_to,
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

    # Dispatch by action type
    if suggestion.action == SuggestedAction.ADVANCE_STAGE:
        if suggestion.stage_id and suggestion.target_state:
            await svc.advance_stage(suggestion.stage_id, StageState(suggestion.target_state), email)
    elif suggestion.action == SuggestedAction.MARK_COLD:
        if suggestion.stage_id:
            await svc.mark_cold(suggestion.stage_id, email)
    elif suggestion.action == SuggestedAction.LINK_THREAD:
        target_loop_id = suggestion.loop_id or suggestion.extracted_entities.get("target_loop_id")
        if target_loop_id and suggestion.gmail_thread_id:
            await svc.link_thread(target_loop_id, suggestion.gmail_thread_id, None, email)

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


async def _handle_unknown(body: AddonRequest, svc: LoopService, email: str, **_):
    fn = body.common_event_object.invoked_function if body.common_event_object else None
    logger.warning("Unknown invokedFunction: %s", fn)
    board = await svc.get_status_board(email)
    return build_drafts_tab(board)


_ACTION_HANDLERS = {
    "show_create_form": _handle_show_create_form,
    "create_loop": _handle_create_loop,
    "view_loop": _handle_view_loop,
    "advance_stage": _handle_advance_stage,
    "mark_cold": _handle_mark_cold,
    "show_revive": _handle_show_revive,
    "revive_stage": _handle_revive_stage,
    "add_stage": _handle_add_stage,
    "compose_email": _handle_compose_email,
    "send_email": _handle_send_email,
    "link_thread": _handle_link_thread,
    "show_add_time_slot": _handle_show_add_time_slot,
    "save_time_slot": _handle_save_time_slot,
    "show_drafts_tab": _handle_show_drafts_tab,
    "show_status_tab": _handle_show_status_tab,
    "forward_thread": _handle_forward_thread,
    "send_inline_email": _handle_send_inline_email,
    "edit_actors": _handle_show_drafts_tab,  # TODO: implement edit actors form
    "view_draft": _handle_view_draft,
    "edit_draft": _handle_edit_draft,
    "send_draft": _handle_send_draft,
    "discard_draft": _handle_discard_draft,
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
