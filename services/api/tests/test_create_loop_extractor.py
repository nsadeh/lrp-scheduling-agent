"""Tests for the manual-path create-loop field extractor.

Covers the three building blocks from rfcs/rfc-infer-create-loop-fields.md:
  - _merge_prefill: extractor overlays deterministic prefill (never overwrites).
  - _coerce_create_loop_action_data: classifier hook parallel-writes typed
    CreateLoopExtraction to action_data for CREATE_LOOP suggestions.
  - _handle_show_create_form: banner + prefill on extractor success;
    deterministic-only fallback on extractor error.
"""

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from api.addon.routes import _merge_prefill
from api.classifier.models import (
    CreateLoopExtraction,
)
from api.main import app
from api.scheduling.models import StatusBoard

_TEST_EMAIL = "coordinator@longridgepartners.com"
_jwt_payload = base64.urlsafe_b64encode(json.dumps({"email": _TEST_EMAIL}).encode()).decode()
_FAKE_USER_ID_TOKEN = f"header.{_jwt_payload}.signature"


# ---------------------------------------------------------------------------
# _merge_prefill
# ---------------------------------------------------------------------------


class TestMergePrefill:
    def test_extractor_fills_nulls_only(self):
        deterministic = CreateLoopExtraction(
            client_name="Jane Deterministic",
            client_email="jane@acme.com",
            cm_name="Cecil CM",
            cm_email="cecil@acme.com",
        )
        extracted = CreateLoopExtraction(
            candidate_name="Claire Candidate",
            client_name="Jane Extracted",  # should lose to deterministic
            client_email="other@acme.com",  # should lose to deterministic
            client_company="Acme Capital",
            recruiter_name="Ruth Recruiter",
        )
        merged = _merge_prefill(deterministic, extracted)

        # Deterministic wins where set
        assert merged.client_name == "Jane Deterministic"
        assert merged.client_email == "jane@acme.com"
        assert merged.cm_name == "Cecil CM"
        assert merged.cm_email == "cecil@acme.com"
        # Extractor fills fields with no deterministic counterpart
        assert merged.candidate_name == "Claire Candidate"
        assert merged.client_company == "Acme Capital"
        # Extractor fills blanks where deterministic was null
        assert merged.recruiter_name == "Ruth Recruiter"
        assert merged.recruiter_email is None

    def test_both_empty(self):
        merged = _merge_prefill(CreateLoopExtraction(), CreateLoopExtraction())
        assert merged.model_dump() == CreateLoopExtraction().model_dump()

    def test_extractor_null_preserves_deterministic(self):
        deterministic = CreateLoopExtraction(client_email="jane@acme.com")
        extracted = CreateLoopExtraction()  # all None
        merged = _merge_prefill(deterministic, extracted)
        assert merged.client_email == "jane@acme.com"


# ---------------------------------------------------------------------------
# _handle_show_create_form end-to-end (extractor wiring)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_scheduling():
    svc = AsyncMock()
    svc.get_status_board = AsyncMock(return_value=StatusBoard())
    svc.find_loop_by_thread = AsyncMock(return_value=None)
    svc.get_contact_by_email = AsyncMock(return_value=None)
    svc.get_client_contact_by_email = AsyncMock(return_value=None)
    # _handle_show_create_form pokes svc._gmail for get_message/get_thread.
    # MagicMock auto-creates attributes, including `.name`, which is truthy —
    # so set `.name = ""` explicitly on `from_` (otherwise `msg.from_.name or None`
    # captures the MagicMock as the value).
    from_ = MagicMock()
    from_.name = ""
    from_.email = "jane@acme.com"
    msg = MagicMock()
    msg.from_ = from_
    msg.cc = []
    msg.subject = "Intro with Claire"
    thread = MagicMock()
    thread_msg = MagicMock()
    thread_msg.id = "msg-1"
    thread.messages = [thread_msg]
    gmail = AsyncMock()
    gmail.get_message = AsyncMock(return_value=msg)
    gmail.get_thread = AsyncMock(return_value=thread)
    svc._gmail = gmail
    return svc


@pytest.fixture
def mock_overview():
    from api.overview.service import OverviewService

    svc = AsyncMock(spec=OverviewService)
    svc.get_overview_data = AsyncMock(return_value=[])
    svc.get_thread_overview_data = AsyncMock(return_value=[])
    return svc


@pytest.fixture
async def client(mock_scheduling, mock_overview):
    app.state.scheduling = mock_scheduling
    app.state.overview_service = mock_overview
    # Extractor prereqs: both must be non-None for extraction to run.
    app.state.llm_service = MagicMock()
    app.state.langfuse = MagicMock()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _show_create_form_event(**extra_params):
    params = {
        "action_name": "show_create_form",
        "gmail_thread_id": "thread-xyz",
        "message_id": "msg-1",
        **extra_params,
    }
    return {
        "commonEventObject": {
            "hostApp": "GMAIL",
            "platform": "WEB",
            "parameters": params,
        },
        "authorizationEventObject": {"userIdToken": _FAKE_USER_ID_TOKEN},
    }


def _form_inputs(card: dict) -> dict[str, str | None]:
    inputs: dict[str, str | None] = {}
    for section in card["sections"]:
        for widget in section["widgets"]:
            ti = widget.get("textInput")
            if ti:
                inputs[ti["name"]] = ti.get("value")
    return inputs


def _banner_text(card: dict) -> str | None:
    """Return the first informational banner's raw text, if present."""
    if not card["sections"]:
        return None
    first = card["sections"][0]
    widget = first["widgets"][0]
    tp = widget.get("textParagraph")
    if tp and tp["text"].startswith("<i>"):
        return tp["text"]
    return None


class TestShowCreateFormExtractor:
    async def test_happy_path_renders_banner_and_prefills(
        self, client: AsyncClient, mock_scheduling
    ):
        extracted = CreateLoopExtraction(
            candidate_name="Claire Candidate",
            client_company="Acme Capital",
            recruiter_name="Ruth Recruiter",
            recruiter_email="ruth@lrp.com",
        )
        with patch(
            "api.addon.routes.extract_create_loop_fields",
            new=AsyncMock(return_value=extracted),
        ):
            resp = await client.post("/addon/action", json=_show_create_form_event())

        assert resp.status_code == 200
        card = resp.json()["action"]["navigations"][0]["updateCard"]
        inputs = _form_inputs(card)

        # Extractor fields present
        assert inputs["candidate_name"] == "Claire Candidate"
        assert inputs["client_company"] == "Acme Capital"
        assert inputs["recruiter_name"] == "Ruth Recruiter"
        assert inputs["recruiter_email"] == "ruth@lrp.com"
        # Deterministic field (from get_message mock) is preserved
        assert inputs["client_email"] == "jane@acme.com"
        # Banner rendered
        banner = _banner_text(card)
        assert banner is not None and "thread" in banner.lower()

    async def test_error_falls_back_to_deterministic_no_banner(
        self, client: AsyncClient, mock_scheduling
    ):
        with patch(
            "api.addon.routes.extract_create_loop_fields",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            resp = await client.post("/addon/action", json=_show_create_form_event())

        assert resp.status_code == 200
        card = resp.json()["action"]["navigations"][0]["updateCard"]
        inputs = _form_inputs(card)
        # Deterministic prefill still works
        assert inputs["client_email"] == "jane@acme.com"
        # Candidate came only from the extractor — must be empty on fallback
        assert not inputs.get("candidate_name")
        # No informational banner on failure
        assert _banner_text(card) is None

    async def test_classifier_prefill_skips_extractor(self, client: AsyncClient, mock_scheduling):
        """When the overview suggestion card passes prefill_* params, we must
        not re-run extraction — the classifier already did the work.
        """
        extractor = AsyncMock(return_value=CreateLoopExtraction(candidate_name="Should-Not-Use"))
        with patch("api.addon.routes.extract_create_loop_fields", new=extractor):
            resp = await client.post(
                "/addon/action",
                json=_show_create_form_event(
                    prefill_candidate_name="Classifier Claire",
                    prefill_client_email="classifier@acme.com",
                ),
            )

        assert resp.status_code == 200
        card = resp.json()["action"]["navigations"][0]["updateCard"]
        inputs = _form_inputs(card)
        assert inputs["candidate_name"] == "Classifier Claire"
        assert inputs["client_email"] == "classifier@acme.com"
        # Extractor should NOT have been called — classifier data is authoritative
        extractor.assert_not_called()
        # No banner either — banner is reserved for the manual AI-assisted path
        assert _banner_text(card) is None
