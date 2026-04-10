"""Google Workspace Add-on HTTP endpoints.

Google POSTs to these routes when a coordinator interacts with the Gmail sidebar.
Token verification is applied at the router level via Depends().
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from api.addon.auth import verify_google_addon_token
from api.addon.models import (
    ActionResponse,
    AddonRequest,
    CardResponse,
    PushCard,
    UpdateCard,
)
from api.agent.cards import build_answer_form, build_suggestion_card
from api.gmail.exceptions import GmailValidationError
from api.scheduling.cards import (
    build_add_time_slot_form,
    build_auth_required,
    build_compose_email,
    build_contextual_linked,
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
    dependencies=[Depends(verify_google_addon_token)],
)


def _get_scheduling(request: Request) -> LoopService:
    return request.app.state.scheduling


def _get_agent_service(request: Request):
    """Get AgentService from app state, or None if not initialized."""
    return getattr(request.app.state, "agent_service", None)


def _get_user_email(body: AddonRequest) -> str | None:
    """Extract coordinator email from the add-on request.

    Google sends a ``userIdToken`` (OIDC JWT) in the ``authorizationEventObject``
    on ALL request types (homepage, on-message, actions) when the deployment
    manifest includes the ``userinfo.email`` scope.

    The token signature is verified against Google's public keys to prevent
    coordinator impersonation via a forged JWT.

    Returns None if the token is missing, malformed, or fails verification.
    """
    auth = body.authorization_event_object
    if not auth or not auth.user_id_token:
        return None

    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    try:
        claims = id_token.verify_oauth2_token(
            auth.user_id_token,
            google_requests.Request(),
        )
        return claims.get("email")
    except Exception:
        logger.warning("Could not verify userIdToken", exc_info=True)
        return None


def _normalize_gmail_id(raw_id: str | None) -> str | None:
    """Convert Google add-on IDs to Gmail API hex format.

    Google's add-on framework sends IDs like ``thread-f:1862089055444310745``
    and ``msg-f:1862089055444310745`` (decimal), but the Gmail API uses hex
    strings like ``19d7782151e69ad9``. Our database stores the hex format.
    """
    if not raw_id:
        return None
    # Already hex format (from Gmail API or our own test requests)
    if not raw_id.startswith(("thread-f:", "msg-f:", "msg-a:")):
        return raw_id
    # Extract decimal part and convert to hex
    decimal_str = raw_id.split(":", 1)[1]
    return format(int(decimal_str), "x")


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


async def _check_gmail_auth(request: Request, user_email: str | None) -> CardResponse | None:
    """Check if the coordinator has stored Gmail credentials.

    Returns an auth-required card if not authenticated, or None if authorized.
    If user_email is None (couldn't identify user), returns auth card.
    """
    if not user_email:
        base = str(request.url).rsplit("/addon/", 1)[0]
        auth_url = f"{base}/addon/oauth/start"
        return build_auth_required(auth_url)
    gmail = getattr(request.app.state, "gmail", None)
    if not gmail:
        return None  # GmailClient not configured — skip auth check
    has = await gmail.has_credentials(user_email)
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


def _as_push(response: CardResponse) -> CardResponse:
    """Convert updateCard navigations to pushCard for initial triggers."""
    navigations = [
        PushCard(push_card=nav.update_card) if isinstance(nav, UpdateCard) else nav
        for nav in response.action.navigations
    ]
    return CardResponse(action=ActionResponse(navigations=navigations))


@addon_router.post("/homepage")
async def addon_homepage(body: AddonRequest, request: Request) -> dict:
    """Homepage trigger — status board showing all active loops."""
    _ensure_action_url(request)
    email = _get_user_email(body)

    auth_card = await _check_gmail_auth(request, email)
    if auth_card:
        return _as_push(auth_card).model_dump(by_alias=True, exclude_none=True)

    svc = _get_scheduling(request)
    board = await svc.get_status_board(email)

    agent_svc = _get_agent_service(request)
    pending = await agent_svc.get_pending_for_coordinator(email) if agent_svc else []

    return _as_push(build_drafts_tab(board, pending_suggestions=pending)).model_dump(
        by_alias=True, exclude_none=True
    )


@addon_router.post("/on-message")
async def addon_on_message(body: AddonRequest, request: Request) -> dict:
    """Contextual trigger — check if this email thread is linked to a loop."""
    _ensure_action_url(request)
    email = _get_user_email(body)

    auth_card = await _check_gmail_auth(request, email)
    if auth_card:
        return _as_push(auth_card).model_dump(by_alias=True, exclude_none=True)

    svc = _get_scheduling(request)
    thread_id = None
    message_id = None
    if body.gmail:
        thread_id = _normalize_gmail_id(body.gmail.thread_id)
        message_id = _normalize_gmail_id(body.gmail.message_id)

    if not thread_id:
        board = await svc.get_status_board(email)
        return _as_push(build_drafts_tab(board)).model_dump(by_alias=True, exclude_none=True)

    loop = await svc.find_loop_by_thread(thread_id)

    # Check for a pending agent suggestion on this thread
    agent_svc = _get_agent_service(request)
    suggestion = None
    draft = None
    if agent_svc:
        suggestion = await agent_svc.get_latest_for_thread(thread_id, email)
        if suggestion and suggestion.status == "pending":
            draft = await agent_svc.get_draft_for_suggestion(suggestion.id)
        else:
            suggestion = None  # Only show pending suggestions

    # If there's a pending suggestion, show the suggestion card
    if suggestion:
        loop_title = loop.title if loop else None
        card = build_suggestion_card(suggestion, draft=draft, loop_title=loop_title)
    elif loop:
        card = build_contextual_linked(loop)
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

    # New suggestion action handlers need request for AgentService access.
    # Route them separately to avoid modifying all existing handler signatures.
    suggestion_handler = _SUGGESTION_HANDLERS.get(fn or "")
    handler = suggestion_handler or _ACTION_HANDLERS.get(fn or "", _handle_unknown)
    try:
        if suggestion_handler:
            card = await handler(body, svc, email, request)
        else:
            card = await handler(body, svc, email)
    except GmailValidationError as exc:
        logger.warning("Gmail validation error in action %s: %s", fn, exc)
        card = build_error_card(str(exc))
    return card.model_dump(by_alias=True, exclude_none=True)


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


async def _handle_show_create_form(body: AddonRequest, svc: LoopService, email: str):
    gmail_thread_id = _get_param(body, "gmail_thread_id")
    message_id = _get_param(body, "message_id")

    # Try to pre-fill from message metadata via Gmail API
    prefill_client_name = None
    prefill_client_email = None
    prefill_cm_name = None
    prefill_cm_email = None
    gmail_subject = None

    if message_id and svc._gmail:
        try:
            msg = await svc._gmail.get_message(email, message_id)
            if msg.from_:
                prefill_client_name = msg.from_.name or ""
                prefill_client_email = msg.from_.email
            if msg.cc:
                prefill_cm_name = msg.cc[0].name or ""
                prefill_cm_email = msg.cc[0].email
            gmail_subject = msg.subject
        except Exception:
            logger.warning("Could not fetch message %s for pre-fill", message_id, exc_info=True)

    return build_create_loop_form(
        gmail_thread_id=gmail_thread_id,
        gmail_subject=gmail_subject,
        prefill_client_name=prefill_client_name,
        prefill_client_email=prefill_client_email,
        prefill_cm_name=prefill_cm_name,
        prefill_cm_email=prefill_cm_email,
    )


async def _handle_create_loop(body: AddonRequest, svc: LoopService, email: str):
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
    return build_loop_detail(loop)


async def _handle_view_loop(body: AddonRequest, svc: LoopService, email: str):
    loop_id = _get_param(body, "loop_id")
    if not loop_id:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)
    loop = await svc.get_loop(loop_id)
    return build_loop_detail(loop)


async def _handle_advance_stage(body: AddonRequest, svc: LoopService, email: str):
    stage_id = _get_param(body, "stage_id")
    to_state = _get_param(body, "to_state")
    if not stage_id or not to_state:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    stage = await svc.advance_stage(stage_id, StageState(to_state), email)
    loop = await svc.get_loop(stage.loop_id)
    return build_loop_detail(loop)


async def _handle_mark_cold(body: AddonRequest, svc: LoopService, email: str):
    stage_id = _get_param(body, "stage_id")
    if not stage_id:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    stage = await svc.mark_cold(stage_id, email)
    loop = await svc.get_loop(stage.loop_id)
    return build_loop_detail(loop)


async def _handle_show_revive(body: AddonRequest, svc: LoopService, email: str):
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


async def _handle_revive_stage(body: AddonRequest, svc: LoopService, email: str):
    stage_id = _get_param(body, "stage_id")
    to_state = _get_form_value(body, "revive_to_state")
    if not stage_id or not to_state:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    stage = await svc.revive_stage(stage_id, StageState(to_state), email)
    loop = await svc.get_loop(stage.loop_id)
    return build_loop_detail(loop)


async def _handle_add_stage(body: AddonRequest, svc: LoopService, email: str):
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


async def _handle_compose_email(body: AddonRequest, svc: LoopService, email: str):
    stage_id = _get_param(body, "stage_id")
    loop_id = _get_param(body, "loop_id")
    if not stage_id or not loop_id:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    loop = await svc.get_loop(loop_id)
    stage = next((s for s in loop.stages if s.id == stage_id), None)
    if not stage:
        return build_loop_detail(loop)

    # Determine recipient based on state
    if stage.state == StageState.NEW and loop.recruiter and loop.recruiter.email:
        to_email = loop.recruiter.email
        subject = f"Re: {loop.title} - Availability Request"
    elif (
        stage.state == StageState.AWAITING_CANDIDATE
        and loop.client_contact
        and loop.client_contact.email
    ):
        to_email = loop.client_contact.email
        subject = f"Re: {loop.title} - Candidate Availability"
    else:
        to_email = ""
        subject = f"Re: {loop.title}"

    gmail_thread_id = loop.email_threads[0].gmail_thread_id if loop.email_threads else None
    return build_compose_email(loop, stage, to_email, subject, gmail_thread_id)


async def _handle_send_email(body: AddonRequest, svc: LoopService, email: str):
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


async def _handle_link_thread(body: AddonRequest, svc: LoopService, email: str):
    loop_id = _get_param(body, "loop_id")
    gmail_thread_id = body.gmail.thread_id if body.gmail else None
    if not loop_id or not gmail_thread_id:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    await svc.link_thread(loop_id, gmail_thread_id, None, email)
    loop = await svc.get_loop(loop_id)
    return build_loop_detail(loop)


async def _handle_show_add_time_slot(body: AddonRequest, svc: LoopService, email: str):
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


async def _handle_save_time_slot(body: AddonRequest, svc: LoopService, email: str):
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


async def _handle_show_drafts_tab(body: AddonRequest, svc: LoopService, email: str):
    board = await svc.get_status_board(email)
    return build_drafts_tab(board)


async def _handle_show_status_tab(body: AddonRequest, svc: LoopService, email: str):
    board = await svc.get_status_board(email)
    return build_status_board(board)


async def _handle_forward_thread(body: AddonRequest, svc: LoopService, email: str):
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


async def _handle_send_inline_email(body: AddonRequest, svc: LoopService, email: str):
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


async def _handle_unknown(body: AddonRequest, svc: LoopService, email: str):
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
}


# ---------------------------------------------------------------------------
# Agent suggestion action handlers
# ---------------------------------------------------------------------------


async def _handle_approve_suggestion(
    body: AddonRequest, svc: LoopService, email: str, request: Request
):
    """Approve a suggestion: send draft (if any) and mark accepted."""
    suggestion_id = _get_param(body, "suggestion_id")
    agent_svc = _get_agent_service(request)
    if not agent_svc or not suggestion_id:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    suggestion = await agent_svc.get_suggestion(suggestion_id)
    if not suggestion:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    # If there's a draft, send it
    draft = await agent_svc.get_draft_for_suggestion(suggestion_id)
    if draft and draft.draft_to and suggestion.loop_id and suggestion.stage_id:
        await svc.send_email(
            loop_id=suggestion.loop_id,
            stage_id=suggestion.stage_id,
            coordinator_email=email,
            to=draft.draft_to,
            subject=draft.draft_subject,
            body=draft.draft_body,
            in_reply_to=draft.in_reply_to,
        )

    # Mark suggestion as accepted
    await agent_svc.resolve_suggestion(suggestion_id, status="accepted")

    # Return updated loop view or status board
    if suggestion.loop_id:
        loop = await svc.get_loop(suggestion.loop_id)
        return build_loop_detail(loop)
    board = await svc.get_status_board(email)
    return build_drafts_tab(board)


async def _handle_edit_suggestion(
    body: AddonRequest, svc: LoopService, email: str, request: Request
):
    """Open compose form pre-filled with agent's draft."""
    suggestion_id = _get_param(body, "suggestion_id")
    agent_svc = _get_agent_service(request)
    if not agent_svc or not suggestion_id:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    suggestion = await agent_svc.get_suggestion(suggestion_id)
    draft = await agent_svc.get_draft_for_suggestion(suggestion_id)
    if not suggestion or not draft:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    # Pre-fill the compose form with the agent's draft
    return build_compose_email(
        stage_id=suggestion.stage_id or "",
        loop_id=suggestion.loop_id or "",
        to=draft.draft_to[0] if draft.draft_to else "",
        subject=draft.draft_subject,
        body=draft.draft_body,
        gmail_thread_id=suggestion.gmail_thread_id,
    )


async def _handle_reject_suggestion(
    body: AddonRequest, svc: LoopService, email: str, request: Request
):
    """Dismiss a suggestion."""
    suggestion_id = _get_param(body, "suggestion_id")
    agent_svc = _get_agent_service(request)
    if agent_svc and suggestion_id:
        feedback = _get_form_value(body, "feedback")
        await agent_svc.resolve_suggestion(
            suggestion_id, status="rejected", coordinator_feedback=feedback
        )
    board = await svc.get_status_board(email)
    return build_drafts_tab(board)


async def _handle_view_suggestion(
    body: AddonRequest, svc: LoopService, email: str, request: Request
):
    """View a specific suggestion card (from Actions tab list)."""
    suggestion_id = _get_param(body, "suggestion_id")
    agent_svc = _get_agent_service(request)
    if not agent_svc or not suggestion_id:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    suggestion = await agent_svc.get_suggestion(suggestion_id)
    if not suggestion:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    draft = await agent_svc.get_draft_for_suggestion(suggestion_id)
    loop_title = None
    if suggestion.loop_id:
        loop = await svc.get_loop(suggestion.loop_id)
        loop_title = loop.title if loop else None

    return build_suggestion_card(suggestion, draft=draft, loop_title=loop_title)


async def _handle_answer_suggestion(
    body: AddonRequest, svc: LoopService, email: str, request: Request
):
    """Show the answer form for an ask_coordinator suggestion."""
    suggestion_id = _get_param(body, "suggestion_id")
    agent_svc = _get_agent_service(request)
    if not agent_svc or not suggestion_id:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    suggestion = await agent_svc.get_suggestion(suggestion_id)
    if not suggestion:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    return build_answer_form(suggestion)


async def _handle_accept_create_loop(
    body: AddonRequest, svc: LoopService, email: str, request: Request
):
    """Accept a create_loop suggestion — open pre-filled loop form."""
    suggestion_id = _get_param(body, "suggestion_id")
    agent_svc = _get_agent_service(request)
    if not agent_svc or not suggestion_id:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    suggestion = await agent_svc.get_suggestion(suggestion_id)
    if not suggestion or not suggestion.prefilled_data:
        board = await svc.get_status_board(email)
        return build_drafts_tab(board)

    prefilled = suggestion.prefilled_data
    await agent_svc.resolve_suggestion(suggestion_id, status="accepted")

    # Map agent-extracted fields to the form's prefill parameters.
    # The agent may not have all fields — the coordinator fills in the rest.
    return build_create_loop_form(
        gmail_thread_id=suggestion.gmail_thread_id,
        gmail_subject=prefilled.get("subject"),
        prefill_candidate_name=prefilled.get("candidate_name"),
        prefill_client_name=prefilled.get("client_name") or prefilled.get("client_contact"),
        prefill_client_email=prefilled.get("client_email"),
        prefill_client_company=prefilled.get("client_company"),
        prefill_recruiter_name=prefilled.get("recruiter_name"),
        prefill_recruiter_email=prefilled.get("recruiter_email"),
        prefill_cm_name=prefilled.get("cm_name"),
        prefill_cm_email=prefilled.get("cm_email"),
        prefill_first_stage=prefilled.get("round"),
    )


_SUGGESTION_HANDLERS = {
    "approve_suggestion": _handle_approve_suggestion,
    "edit_suggestion": _handle_edit_suggestion,
    "reject_suggestion": _handle_reject_suggestion,
    "view_suggestion": _handle_view_suggestion,
    "answer_suggestion": _handle_answer_suggestion,
    "accept_create_loop": _handle_accept_create_loop,
}


# ---------------------------------------------------------------------------
# OAuth flow (no Google add-on token verification — browser-based)
# ---------------------------------------------------------------------------

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

    return HTMLResponse(
        "<html><body><h2>Gmail access authorized.</h2>"
        "<p>You can close this tab and return to Gmail.</p>"
        "</body></html>"
    )
