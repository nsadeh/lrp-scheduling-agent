"""Tests for the recruiter directory autocomplete feature.

Covers:
- SuggestionsResponse / autoCompleteAction model serialization
- format/parse round-trip for "Name <email>" encoding
- /addon/directory/search endpoint behavior
- recruiter_selected action handler (onChangeAction)
- autoCompleteAction wiring in both create-loop forms
- photo_url fetch integration in create_loop
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from api.addon.directory import (
    DirectoryPerson,
    _match_rank,
    _matches,
    _parse_person,
    _tokenize_query,
    search_directory,
)
from api.addon.models import (
    OnClickAction,
    SuggestionItem,
    Suggestions,
    SuggestionsResponse,
    TextInput,
)
from api.addon.routes import format_directory_suggestion, parse_name_email
from api.main import app
from api.scheduling.cards import (
    build_create_loop_form,
    directory_search_url,
    set_action_url,
)
from api.scheduling.models import Contact

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TEST_EMAIL = "coord@longridgepartners.com"
_jwt_payload = base64.urlsafe_b64encode(json.dumps({"email": _TEST_EMAIL}).encode()).decode()
_FAKE_USER_ID_TOKEN = f"header.{_jwt_payload}.signature"


@pytest.fixture(autouse=True)
def _action_url_set():
    """Ensure the action URL is set for form rendering tests."""
    set_action_url("https://test.example.com/addon/action")


@pytest.fixture
def mock_scheduling():
    svc = AsyncMock()
    svc.get_contact_by_email = AsyncMock(return_value=None)
    svc.get_client_contact_by_email = AsyncMock(return_value=None)
    return svc


@pytest.fixture
def mock_gmail():
    """Mock GmailClient with load_credentials callable."""
    g = MagicMock()
    g._token_store = MagicMock()
    g._token_store.load_credentials = AsyncMock(return_value=MagicMock())
    return g


@pytest.fixture
async def client(mock_scheduling, mock_gmail):
    # Snapshot prior values so we can restore on teardown and avoid leaking
    # mocks into tests that run later in the session.
    prior_scheduling = getattr(app.state, "scheduling", None)
    prior_gmail = getattr(app.state, "gmail", None)
    prior_overview = getattr(app.state, "overview_service", None)

    app.state.scheduling = mock_scheduling
    app.state.gmail = mock_gmail
    from unittest.mock import AsyncMock as _AsyncMock

    app.state.overview_service = _AsyncMock()
    app.state.overview_service.get_overview_data = _AsyncMock(return_value=[])
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        # Pop attributes we added; restore prior values we overwrote.
        if prior_scheduling is None:
            if hasattr(app.state, "scheduling"):
                delattr(app.state, "scheduling")
        else:
            app.state.scheduling = prior_scheduling
        if prior_gmail is None:
            if hasattr(app.state, "gmail"):
                delattr(app.state, "gmail")
        else:
            app.state.gmail = prior_gmail
        if prior_overview is None:
            if hasattr(app.state, "overview_service"):
                delattr(app.state, "overview_service")
        else:
            app.state.overview_service = prior_overview


# ---------------------------------------------------------------------------
# Parse / format round-trip
# ---------------------------------------------------------------------------


class TestFormatParseRoundtrip:
    def test_format_includes_name_and_email(self):
        assert format_directory_suggestion("Sarah Chen", "sarah@lrp.com") == (
            "Sarah Chen <sarah@lrp.com>"
        )

    def test_format_strips_whitespace(self):
        assert format_directory_suggestion("  Sarah  ", "s@x.com") == "Sarah <s@x.com>"

    def test_format_falls_back_to_email_local_part_on_missing_name(self):
        assert format_directory_suggestion("", "sarah@lrp.com") == "sarah <sarah@lrp.com>"

    def test_parse_handles_standard_shape(self):
        assert parse_name_email("Sarah Chen <sarah@lrp.com>") == ("Sarah Chen", "sarah@lrp.com")

    def test_parse_handles_extra_whitespace(self):
        assert parse_name_email("  Sarah Chen  <  sarah@lrp.com  >  ") == (
            "Sarah Chen",
            "sarah@lrp.com",
        )

    def test_parse_returns_none_on_plain_string(self):
        assert parse_name_email("Sarah") is None

    def test_parse_returns_none_on_just_email(self):
        # Email without surrounding name+brackets should not match — the
        # whole purpose of the format is the embedded email marker.
        assert parse_name_email("sarah@lrp.com") is None

    def test_parse_returns_none_on_empty(self):
        assert parse_name_email("") is None
        assert parse_name_email(None) is None  # type: ignore[arg-type]

    def test_parse_rejects_angle_brackets_in_name(self):
        """A crafted ``"A <script> B <real@x.com>"`` must not match — the
        angle-bracket content would otherwise leak into the persisted name."""
        assert parse_name_email("A <script> B <real@x.com>") is None
        assert parse_name_email("Sarah > <s@x.com>") is None

    def test_roundtrip(self):
        # Format -> parse should give back the name and email we put in
        formatted = format_directory_suggestion("Sarah Chen", "sarah@longridgepartners.com")
        parsed = parse_name_email(formatted)
        assert parsed == ("Sarah Chen", "sarah@longridgepartners.com")


# ---------------------------------------------------------------------------
# Pydantic model shapes
# ---------------------------------------------------------------------------


class TestSuggestionsModels:
    def test_suggestions_response_serializes_to_googles_shape(self):
        """The envelope Google's GetAutocompletionResponse RPC expects.

        Shape: ``{"autoComplete": {"items": [{"text": "..."}]}}``.
        The top-level field is ``autoComplete`` (camelCase), *not*
        ``suggestions`` — the inner type is named ``Suggestions`` in the
        Apps Script SDK but the wire JSON uses ``autoComplete``.
        """
        r = SuggestionsResponse(
            auto_complete=Suggestions(
                items=[
                    SuggestionItem(text="Sarah <s@x.com>"),
                    SuggestionItem(text="Bob <b@x.com>"),
                ]
            )
        )
        serialized = r.model_dump(by_alias=True, exclude_none=True)
        assert serialized == {
            "autoComplete": {
                "items": [
                    {"text": "Sarah <s@x.com>"},
                    {"text": "Bob <b@x.com>"},
                ]
            }
        }

    def test_text_input_autocomplete_action_renders_at_camelcase_key(self):
        """Google expects camelCase ``autoCompleteAction`` in the JSON body."""
        ti = TextInput(
            name="x",
            label="y",
            auto_complete_action=OnClickAction(function="https://example.com"),
        )
        payload = ti.model_dump(by_alias=True, exclude_none=True)
        assert "autoCompleteAction" in payload
        assert payload["autoCompleteAction"]["function"] == "https://example.com"

    def test_text_input_on_change_action_renders_at_camelcase_key(self):
        ti = TextInput(
            name="x",
            label="y",
            on_change_action=OnClickAction(function="https://example.com"),
        )
        payload = ti.model_dump(by_alias=True, exclude_none=True)
        assert "onChangeAction" in payload
        assert payload["onChangeAction"]["function"] == "https://example.com"


# ---------------------------------------------------------------------------
# People API DTO parsing
# ---------------------------------------------------------------------------


class TestDirectoryPersonParsing:
    def test_parses_well_formed_response(self):
        p = _parse_person(
            {
                "resourceName": "people/c123",
                "names": [{"displayName": "Sarah Chen"}],
                "emailAddresses": [{"value": "sarah@lrp.com"}],
                "photos": [{"url": "https://lh3.googleusercontent.com/abc"}],
            }
        )
        assert p is not None
        assert p.display_name == "Sarah Chen"
        assert p.email == "sarah@lrp.com"
        assert p.photo_url == "https://lh3.googleusercontent.com/abc"
        assert p.resource_name == "people/c123"

    def test_returns_none_when_no_email(self):
        """Directory result without an email is useless to us — skip it."""
        assert _parse_person({"resourceName": "people/c1", "names": [{"displayName": "X"}]}) is None

    def test_returns_none_when_email_list_is_empty(self):
        assert _parse_person({"emailAddresses": []}) is None

    def test_tolerates_missing_photo(self):
        p = _parse_person(
            {
                "names": [{"displayName": "Sarah"}],
                "emailAddresses": [{"value": "s@lrp.com"}],
            }
        )
        assert p is not None
        assert p.photo_url is None

    def test_tolerates_missing_display_name(self):
        p = _parse_person({"emailAddresses": [{"value": "s@lrp.com"}]})
        assert p is not None
        assert p.display_name == ""
        assert p.email == "s@lrp.com"


# ---------------------------------------------------------------------------
# Query matching — the core logic that replaces searchDirectoryPeople
# ---------------------------------------------------------------------------


def _person(name: str, email: str) -> DirectoryPerson:
    return DirectoryPerson(
        resource_name=f"people/{email}",
        display_name=name,
        email=email,
        photo_url=None,
    )


class TestTokenizeQuery:
    def test_plain_word(self):
        assert _tokenize_query("adam") == ["adam"]

    def test_lowercases(self):
        assert _tokenize_query("Adam") == ["adam"]

    def test_splits_on_at_sign(self):
        """'adam@' → ['adam'] so the query matches emails like adam.smith@lrp.com."""
        assert _tokenize_query("adam@") == ["adam"]

    def test_splits_on_dot(self):
        assert _tokenize_query("sarah.cheng") == ["sarah", "cheng"]

    def test_splits_on_whitespace(self):
        assert _tokenize_query("Sarah Cheng") == ["sarah", "cheng"]

    def test_splits_on_email_shape(self):
        assert _tokenize_query("adam@lrp.com") == ["adam", "lrp", "com"]

    def test_empty_query_returns_empty_list(self):
        assert _tokenize_query("") == []
        assert _tokenize_query("   ") == []
        assert _tokenize_query("@.") == []


class TestMatches:
    def test_name_substring_match(self):
        assert _matches(_person("Adam Smith", "a@lrp.com"), ["adam"])

    def test_email_substring_match(self):
        assert _matches(_person("Someone", "adam@lrp.com"), ["adam"])

    def test_case_insensitive(self):
        assert _matches(_person("Adam Smith", "a@lrp.com"), ["ADAM"]) is False
        # Tokens come from _tokenize_query which lowercases, so uppercase
        # tokens shouldn't occur in practice. But the haystack is always
        # lowercased — confirm that invariant.
        assert _matches(_person("Adam Smith", "a@lrp.com"), ["adam"]) is True

    def test_all_tokens_must_match(self):
        assert _matches(_person("Adam Smith", "a@lrp.com"), ["adam", "smith"])
        assert not _matches(_person("Adam Smith", "a@lrp.com"), ["adam", "jones"])

    def test_matches_across_name_and_email_together(self):
        """Tokens can satisfy via name OR email — they don't have to all be in one field."""
        p = _person("Adam Smith", "adam@lrp.com")
        assert _matches(p, ["smith", "lrp"])

    def test_empty_tokens_matches_everyone(self):
        # An empty token list is vacuously true; callers should guard
        # against this themselves (search_directory does).
        assert _matches(_person("Any", "any@x.com"), [])


class TestMatchRank:
    def test_name_prefix_beats_substring(self):
        name_prefix = _person("Adam Smith", "s@lrp.com")
        substring_only = _person("Sarah Adams", "s@lrp.com")
        assert _match_rank(name_prefix, "adam") < _match_rank(substring_only, "adam")

    def test_email_localpart_prefix_beats_substring(self):
        email_prefix = _person("Someone Else", "adam@lrp.com")
        name_substring = _person("Sarah Adams", "sarah@lrp.com")
        assert _match_rank(email_prefix, "adam") < _match_rank(name_substring, "adam")

    def test_same_rank_sorts_alphabetically(self):
        # Both are prefix matches — ties break by name
        adam = _person("Adam Smith", "adam@lrp.com")
        adrian = _person("Adrian Jones", "adrian@lrp.com")
        # Adam sorts before Adrian
        assert _match_rank(adam, "ad") < _match_rank(adrian, "ad")


# ---------------------------------------------------------------------------
# search_directory integration: listDirectoryPeople + client-side filter
# ---------------------------------------------------------------------------


class TestSearchDirectory:
    """Verify that search_directory fetches the full directory via
    listDirectoryPeople and filters in Python."""

    async def test_filters_by_name_substring(self):
        creds = MagicMock()
        creds.valid = True
        creds.token = "fake-token"

        with patch(
            "api.addon.directory.list_directory",
            new=AsyncMock(
                return_value=[
                    _person("Adam Smith", "adam.smith@lrp.com"),
                    _person("Sarah Cheng", "sarah@lrp.com"),
                    _person("Adrian Jones", "adrian@lrp.com"),
                ]
            ),
        ):
            results = await search_directory(creds, "adam")

        names = [p.display_name for p in results]
        # Adam Smith matches on name prefix; Sarah does not contain "adam"
        assert "Adam Smith" in names
        assert "Sarah Cheng" not in names

    async def test_matches_email_local_part_from_query_with_at(self):
        """The regression that motivated this switch: 'adam@' used to
        return 0 results under searchDirectoryPeople because it required
        a literal-'@' prefix. The list-then-filter path handles it."""
        creds = MagicMock()
        creds.valid = True
        creds.token = "fake-token"

        with patch(
            "api.addon.directory.list_directory",
            new=AsyncMock(return_value=[_person("Adam Smith", "adam.smith@lrp.com")]),
        ):
            results = await search_directory(creds, "adam@")

        assert len(results) == 1
        assert results[0].email == "adam.smith@lrp.com"

    async def test_multi_token_query_requires_all(self):
        creds = MagicMock()
        creds.valid = True
        creds.token = "fake-token"

        with patch(
            "api.addon.directory.list_directory",
            new=AsyncMock(
                return_value=[
                    _person("Sarah Cheng", "sarah.cheng@lrp.com"),
                    _person("Sarah Adams", "sarah@lrp.com"),
                ]
            ),
        ):
            results = await search_directory(creds, "sarah cheng")

        assert len(results) == 1
        assert results[0].display_name == "Sarah Cheng"

    async def test_ranks_prefix_matches_first(self):
        creds = MagicMock()
        creds.valid = True
        creds.token = "fake-token"

        with patch(
            "api.addon.directory.list_directory",
            new=AsyncMock(
                return_value=[
                    _person("Sarah Adams", "sarah@lrp.com"),  # substring on "adam"
                    _person("Adam Smith", "adam@lrp.com"),  # prefix on "adam"
                ]
            ),
        ):
            results = await search_directory(creds, "adam")

        # Prefix match should sort first
        assert results[0].display_name == "Adam Smith"
        assert results[1].display_name == "Sarah Adams"

    async def test_caps_at_page_size(self):
        creds = MagicMock()
        creds.valid = True
        creds.token = "fake-token"
        huge_directory = [_person(f"Person {i}", f"p{i}@lrp.com") for i in range(100)]
        with patch(
            "api.addon.directory.list_directory",
            new=AsyncMock(return_value=huge_directory),
        ):
            results = await search_directory(creds, "person", page_size=5)

        assert len(results) == 5

    async def test_empty_query_returns_empty(self):
        creds = MagicMock()
        creds.valid = True
        list_mock = AsyncMock()
        with patch("api.addon.directory.list_directory", new=list_mock):
            assert await search_directory(creds, "") == []
            assert await search_directory(creds, "   ") == []
        # Shouldn't even fetch the directory for an empty query
        list_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Form wiring — autoCompleteAction + onChangeAction on recruiter fields
# ---------------------------------------------------------------------------


class TestCreateLoopFormAutocomplete:
    def test_recruiter_name_has_autocomplete_action(self):
        data = build_create_loop_form().model_dump(by_alias=True, exclude_none=True)
        card = data["action"]["navigations"][0]["updateCard"]
        recruiter_section = next(s for s in card["sections"] if s.get("header") == "Recruiter")
        name_input = recruiter_section["widgets"][0]["textInput"]
        assert "autoCompleteAction" in name_input
        assert name_input["autoCompleteAction"]["function"].endswith("/addon/directory/search")

    def test_recruiter_email_has_autocomplete_action(self):
        data = build_create_loop_form().model_dump(by_alias=True, exclude_none=True)
        card = data["action"]["navigations"][0]["updateCard"]
        recruiter_section = next(s for s in card["sections"] if s.get("header") == "Recruiter")
        email_input = recruiter_section["widgets"][1]["textInput"]
        assert "autoCompleteAction" in email_input

    def test_recruiter_fields_have_on_change_action(self):
        data = build_create_loop_form().model_dump(by_alias=True, exclude_none=True)
        card = data["action"]["navigations"][0]["updateCard"]
        recruiter_section = next(s for s in card["sections"] if s.get("header") == "Recruiter")
        for widget in recruiter_section["widgets"]:
            ti = widget["textInput"]
            assert "onChangeAction" in ti
            # The onChange callback must name the recruiter_selected handler
            params = ti["onChangeAction"].get("parameters", [])
            param_map = {p["key"]: p["value"] for p in params}
            assert param_map.get("action_name") == "recruiter_selected"

    def test_client_fields_unchanged(self):
        """G4 no-regression: client contact and CM fields stay plain TextInputs."""
        data = build_create_loop_form().model_dump(by_alias=True, exclude_none=True)
        card = data["action"]["navigations"][0]["updateCard"]
        for section in card["sections"]:
            header = section.get("header") or ""
            if header == "Recruiter":
                continue
            for widget in section.get("widgets", []):
                ti = widget.get("textInput")
                if ti is None:
                    continue
                assert (
                    "autoCompleteAction" not in ti
                ), f"section {header!r} field {ti['name']!r} unexpectedly has autocomplete"
                assert "onChangeAction" not in ti

    def test_directory_search_url_is_derived_from_action_url(self):
        set_action_url("https://prod.example.com/addon/action")
        assert directory_search_url() == "https://prod.example.com/addon/directory/search"

    def test_directory_search_url_empty_when_no_action_url(self):
        set_action_url("")
        assert directory_search_url() == ""
        # Restore for other tests
        set_action_url("https://test.example.com/addon/action")


# ---------------------------------------------------------------------------
# /addon/directory/search endpoint
# ---------------------------------------------------------------------------


def _autocomplete_event(
    field: str = "recruiter_name", value: str = "sa", via_form_inputs: bool = True
) -> dict:
    """Build the POST body Google actually sends for an autoCompleteAction.

    The currently-typed text lives under
    ``commonEventObject.formInputs[<name>].stringInputs.value[0]`` in HTTP
    add-ons (same path as any field value). Our endpoint also accepts a
    legacy ``parameters["query"]`` fallback; set via_form_inputs=False to
    exercise that path.
    """
    common: dict = {"hostApp": "GMAIL", "platform": "WEB"}
    if via_form_inputs:
        common["formInputs"] = {field: {"stringInputs": {"value": [value]}}}
    else:
        common["parameters"] = {"query": value}
    return {
        "commonEventObject": common,
        "authorizationEventObject": {"userIdToken": _FAKE_USER_ID_TOKEN},
    }


class TestDirectorySearchEndpoint:
    async def test_empty_query_returns_no_suggestions(self, client: AsyncClient):
        resp = await client.post(
            "/addon/directory/search",
            json=_autocomplete_event(value=""),
        )
        assert resp.status_code == 200
        assert resp.json() == {"autoComplete": {"items": []}}

    async def test_returns_formatted_suggestions_from_form_inputs(
        self, client: AsyncClient, mock_gmail
    ):
        """Primary path — Google sends the typed text via formInputs."""
        with patch(
            "api.addon.routes.search_directory",
            new=AsyncMock(
                return_value=[
                    DirectoryPerson(
                        resource_name="people/c1",
                        display_name="Sarah Chen",
                        email="sarah@lrp.com",
                        photo_url="https://lh3/sarah",
                    ),
                    DirectoryPerson(
                        resource_name="people/c2",
                        display_name="Sam Ray",
                        email="sam@lrp.com",
                        photo_url=None,
                    ),
                ]
            ),
        ):
            resp = await client.post(
                "/addon/directory/search",
                json=_autocomplete_event(field="recruiter_name", value="sa"),
            )

        assert resp.status_code == 200
        texts = [i["text"] for i in resp.json()["autoComplete"]["items"]]
        assert "Sarah Chen <sarah@lrp.com>" in texts
        assert "Sam Ray <sam@lrp.com>" in texts

    async def test_reads_query_from_email_field_too(self, client: AsyncClient, mock_gmail):
        """Coordinator might be typing in the email field instead — same handler
        should accept either recruiter field as the query source."""
        search = AsyncMock(return_value=[])
        with patch("api.addon.routes.search_directory", new=search):
            await client.post(
                "/addon/directory/search",
                json=_autocomplete_event(field="recruiter_email", value="fi"),
            )
        search.assert_awaited_once()
        # Second positional arg to search_directory is the query string
        assert search.await_args.args[1] == "fi"

    async def test_parameters_query_is_fallback(self, client: AsyncClient, mock_gmail):
        """Legacy fallback: if Google ever sends via parameters (or a test
        exercises that path), still extract the query correctly."""
        search = AsyncMock(return_value=[])
        with patch("api.addon.routes.search_directory", new=search):
            await client.post(
                "/addon/directory/search",
                json=_autocomplete_event(value="sa", via_form_inputs=False),
            )
        assert search.await_args.args[1] == "sa"

    async def test_returns_empty_when_people_api_fails(self, client: AsyncClient, mock_gmail):
        """Endpoint must degrade to empty, not 500 — autocomplete can't show errors."""
        with patch(
            "api.addon.routes.search_directory",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            resp = await client.post(
                "/addon/directory/search",
                json=_autocomplete_event(value="sa"),
            )
        assert resp.status_code == 200
        assert resp.json() == {"autoComplete": {"items": []}}

    async def test_returns_empty_when_scope_missing(self, client: AsyncClient, mock_gmail):
        """If the coordinator hasn't re-consented, suggestions return empty —
        the re-consent card surfaces via the show_create_form pre-check instead."""
        from api.gmail.exceptions import GmailScopeError

        mock_gmail._token_store.load_credentials = AsyncMock(
            side_effect=GmailScopeError("missing", missing_scopes=["directory.readonly"])
        )
        resp = await client.post(
            "/addon/directory/search",
            json=_autocomplete_event(value="sa"),
        )
        assert resp.status_code == 200
        assert resp.json() == {"autoComplete": {"items": []}}


# ---------------------------------------------------------------------------
# recruiter_selected onChangeAction handler
# ---------------------------------------------------------------------------


class TestRecruiterSelectedHandler:
    async def test_splits_name_and_email_when_dropped_into_name_field(self, client: AsyncClient):
        event = {
            "commonEventObject": {
                "hostApp": "GMAIL",
                "platform": "WEB",
                "parameters": {"action_name": "recruiter_selected"},
                "formInputs": {
                    "recruiter_name": {"stringInputs": {"value": ["Sarah Chen <sarah@lrp.com>"]}},
                    "recruiter_email": {"stringInputs": {"value": [""]}},
                    "candidate_name": {"stringInputs": {"value": ["Jane Doe"]}},
                },
            },
            "authorizationEventObject": {"userIdToken": _FAKE_USER_ID_TOKEN},
        }
        resp = await client.post("/addon/action", json=event)
        assert resp.status_code == 200
        card = resp.json()["action"]["navigations"][0]["updateCard"]
        inputs = _extract_inputs(card)
        assert inputs["recruiter_name"] == "Sarah Chen"
        assert inputs["recruiter_email"] == "sarah@lrp.com"
        # Other fields preserved
        assert inputs["candidate_name"] == "Jane Doe"

    async def test_splits_when_dropped_into_email_field(self, client: AsyncClient):
        event = {
            "commonEventObject": {
                "hostApp": "GMAIL",
                "platform": "WEB",
                "parameters": {"action_name": "recruiter_selected"},
                "formInputs": {
                    "recruiter_name": {"stringInputs": {"value": [""]}},
                    "recruiter_email": {"stringInputs": {"value": ["Bob Ray <bob@lrp.com>"]}},
                },
            },
            "authorizationEventObject": {"userIdToken": _FAKE_USER_ID_TOKEN},
        }
        resp = await client.post("/addon/action", json=event)
        assert resp.status_code == 200
        inputs = _extract_inputs(resp.json()["action"]["navigations"][0]["updateCard"])
        assert inputs["recruiter_name"] == "Bob Ray"
        assert inputs["recruiter_email"] == "bob@lrp.com"

    async def test_leaves_fields_unchanged_when_manual_typing(self, client: AsyncClient):
        """If the value doesn't match the sentinel, treat as manual typing
        and leave both fields as-is. No round-trip corruption."""
        event = {
            "commonEventObject": {
                "hostApp": "GMAIL",
                "platform": "WEB",
                "parameters": {"action_name": "recruiter_selected"},
                "formInputs": {
                    "recruiter_name": {"stringInputs": {"value": ["Sarah"]}},
                    "recruiter_email": {"stringInputs": {"value": ["sarah@external.com"]}},
                },
            },
            "authorizationEventObject": {"userIdToken": _FAKE_USER_ID_TOKEN},
        }
        resp = await client.post("/addon/action", json=event)
        inputs = _extract_inputs(resp.json()["action"]["navigations"][0]["updateCard"])
        assert inputs["recruiter_name"] == "Sarah"
        assert inputs["recruiter_email"] == "sarah@external.com"


# ---------------------------------------------------------------------------
# create_loop photo_url integration
# ---------------------------------------------------------------------------


class TestCreateLoopPhotoUrl:
    async def test_create_loop_persists_photo_url_for_new_recruiter(
        self, client: AsyncClient, mock_scheduling
    ):
        """When the recruiter doesn't exist yet, we fetch their directory
        photo and pass it to find_or_create_contact."""
        mock_scheduling.find_or_create_client_contact = AsyncMock(
            return_value=MagicMock(id="cli_test")
        )
        mock_scheduling.find_or_create_contact = AsyncMock(return_value=MagicMock(id="con_test"))
        mock_scheduling.get_contact_by_email = AsyncMock(return_value=None)
        mock_scheduling.create_loop = AsyncMock()
        # Avoid triggering the overview refresh path — stub it out
        mock_scheduling.get_coordinator_by_email = AsyncMock(return_value=None)

        with patch(
            "api.addon.routes.search_directory",
            new=AsyncMock(
                return_value=[
                    DirectoryPerson(
                        resource_name="people/c1",
                        display_name="Sarah Chen",
                        email="sarah@lrp.com",
                        photo_url="https://lh3/sarah",
                    )
                ]
            ),
        ):
            event = {
                "commonEventObject": {
                    "hostApp": "GMAIL",
                    "platform": "WEB",
                    "parameters": {"action_name": "create_loop"},
                    "formInputs": {
                        "candidate_name": {"stringInputs": {"value": ["Jane Doe"]}},
                        "client_name": {"stringInputs": {"value": ["Client"]}},
                        "client_email": {"stringInputs": {"value": ["c@acme.com"]}},
                        "client_company": {"stringInputs": {"value": ["Acme"]}},
                        "recruiter_name": {"stringInputs": {"value": ["Sarah Chen"]}},
                        "recruiter_email": {"stringInputs": {"value": ["sarah@lrp.com"]}},
                        "first_stage_name": {"stringInputs": {"value": ["Round 1"]}},
                    },
                },
                "authorizationEventObject": {"userIdToken": _FAKE_USER_ID_TOKEN},
            }
            resp = await client.post("/addon/action", json=event)

        assert resp.status_code == 200
        # The recruiter find_or_create_contact call should have received photo_url
        recruiter_call = None
        for call in mock_scheduling.find_or_create_contact.await_args_list:
            if call.kwargs.get("role") == "recruiter":
                recruiter_call = call
                break
        assert recruiter_call is not None, "find_or_create_contact never called for recruiter"
        assert recruiter_call.kwargs.get("photo_url") == "https://lh3/sarah"

    async def test_create_loop_skips_photo_lookup_when_contact_exists(
        self, client: AsyncClient, mock_scheduling
    ):
        """If the recruiter is already a contact, don't waste a People API
        call — the existing row's photo_url is preserved anyway."""
        mock_scheduling.find_or_create_client_contact = AsyncMock(
            return_value=MagicMock(id="cli_test")
        )
        mock_scheduling.find_or_create_contact = AsyncMock(return_value=MagicMock(id="con_test"))
        mock_scheduling.get_contact_by_email = AsyncMock(
            return_value=Contact(
                id="con_test",
                name="Sarah Chen",
                email="sarah@lrp.com",
                role="recruiter",
                photo_url="https://lh3/sarah-old",
                created_at=datetime.now(UTC),
            )
        )
        mock_scheduling.create_loop = AsyncMock()
        mock_scheduling.get_coordinator_by_email = AsyncMock(return_value=None)

        search_mock = AsyncMock()
        with patch("api.addon.routes.search_directory", new=search_mock):
            event = {
                "commonEventObject": {
                    "hostApp": "GMAIL",
                    "platform": "WEB",
                    "parameters": {"action_name": "create_loop"},
                    "formInputs": {
                        "candidate_name": {"stringInputs": {"value": ["Jane Doe"]}},
                        "client_name": {"stringInputs": {"value": ["Client"]}},
                        "client_email": {"stringInputs": {"value": ["c@acme.com"]}},
                        "client_company": {"stringInputs": {"value": ["Acme"]}},
                        "recruiter_name": {"stringInputs": {"value": ["Sarah Chen"]}},
                        "recruiter_email": {"stringInputs": {"value": ["sarah@lrp.com"]}},
                    },
                },
                "authorizationEventObject": {"userIdToken": _FAKE_USER_ID_TOKEN},
            }
            resp = await client.post("/addon/action", json=event)

        assert resp.status_code == 200
        search_mock.assert_not_called()

    async def test_create_loop_splits_combined_name_email_defensively(
        self, client: AsyncClient, mock_scheduling
    ):
        """If onChangeAction didn't fire (e.g., coordinator clicked Create
        while the field still held "Name <email>"), the handler still
        parses it out instead of persisting the literal string as name."""
        mock_scheduling.find_or_create_client_contact = AsyncMock(
            return_value=MagicMock(id="cli_test")
        )
        mock_scheduling.find_or_create_contact = AsyncMock(return_value=MagicMock(id="con_test"))
        mock_scheduling.get_contact_by_email = AsyncMock(return_value=None)
        mock_scheduling.create_loop = AsyncMock()
        mock_scheduling.get_coordinator_by_email = AsyncMock(return_value=None)

        with patch(
            "api.addon.routes.search_directory",
            new=AsyncMock(return_value=[]),
        ):
            event = {
                "commonEventObject": {
                    "hostApp": "GMAIL",
                    "platform": "WEB",
                    "parameters": {"action_name": "create_loop"},
                    "formInputs": {
                        "candidate_name": {"stringInputs": {"value": ["Jane Doe"]}},
                        "client_name": {"stringInputs": {"value": ["Client"]}},
                        "client_email": {"stringInputs": {"value": ["c@acme.com"]}},
                        "client_company": {"stringInputs": {"value": ["Acme"]}},
                        "recruiter_name": {
                            "stringInputs": {"value": ["Sarah Chen <sarah@lrp.com>"]}
                        },
                        "recruiter_email": {"stringInputs": {"value": [""]}},
                    },
                },
                "authorizationEventObject": {"userIdToken": _FAKE_USER_ID_TOKEN},
            }
            resp = await client.post("/addon/action", json=event)

        assert resp.status_code == 200
        recruiter_call = None
        for call in mock_scheduling.find_or_create_contact.await_args_list:
            if call.kwargs.get("role") == "recruiter":
                recruiter_call = call
                break
        assert recruiter_call is not None
        assert recruiter_call.kwargs["name"] == "Sarah Chen"
        assert recruiter_call.kwargs["email"] == "sarah@lrp.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_inputs(card: dict) -> dict[str, str | None]:
    inputs: dict[str, str | None] = {}
    for section in card.get("sections", []):
        for widget in section.get("widgets", []):
            ti = widget.get("textInput")
            if ti:
                inputs[ti["name"]] = ti.get("value")
    return inputs
