"""Tests for Google Workspace Add-on Pydantic models."""

from api.addon.models import (
    ActionResponse,
    AddonRequest,
    Card,
    CardHeader,
    CardResponse,
    Section,
    TextParagraph,
    TextParagraphWidget,
    UpdateCard,
)


class TestAddonRequest:
    def test_parses_full_google_payload(self):
        """AddonRequest parses a realistic Google event object with camelCase keys."""
        payload = {
            "commonEventObject": {
                "userLocale": "en",
                "hostApp": "GMAIL",
                "platform": "WEB",
                "timeZone": {"id": "America/New_York", "offset": -14400000},
            },
            "authorizationEventObject": {
                "userOAuthToken": "ya29.xxx",
                "userIdToken": "eyJhbGciOi.xxx",
                "systemIdToken": "eyJhbGciOi.yyy",
            },
            "gmail": {
                "messageId": "msg-123",
                "threadId": "thread-456",
                "accessToken": "ya29.zzz",
            },
        }
        req = AddonRequest.model_validate(payload)
        assert req.common_event_object.host_app == "GMAIL"
        assert req.common_event_object.time_zone.id == "America/New_York"
        assert req.authorization_event_object.user_oauth_token == "ya29.xxx"
        assert req.gmail.message_id == "msg-123"
        assert req.gmail.thread_id == "thread-456"

    def test_parses_minimal_payload(self):
        """AddonRequest handles a homepage trigger with no gmail context."""
        payload = {
            "commonEventObject": {
                "hostApp": "GMAIL",
                "platform": "WEB",
            },
        }
        req = AddonRequest.model_validate(payload)
        assert req.common_event_object.host_app == "GMAIL"
        assert req.gmail is None
        assert req.authorization_event_object is None

    def test_accepts_extra_fields(self):
        """Google may add new fields; we should not reject them."""
        payload = {
            "commonEventObject": {
                "hostApp": "GMAIL",
                "platform": "WEB",
                "someNewField": "surprise",
            },
            "brandNewTopLevel": {"nested": True},
        }
        req = AddonRequest.model_validate(payload)
        assert req.common_event_object.host_app == "GMAIL"

    def test_empty_payload(self):
        """An empty payload should still parse (all fields optional)."""
        req = AddonRequest.model_validate({})
        assert req.common_event_object is None
        assert req.gmail is None


class TestCardResponse:
    def test_serializes_to_camel_case(self):
        """CardResponse.model_dump() produces the camelCase keys Google expects."""
        response = CardResponse(
            action=ActionResponse(
                navigations=[
                    UpdateCard(
                        update_card=Card(
                            header=CardHeader(
                                title="Test",
                                image_url="https://example.com/logo.png",
                                image_type="CIRCLE",
                            ),
                            sections=[
                                Section(
                                    widgets=[
                                        TextParagraphWidget(
                                            text_paragraph=TextParagraph(text="Hello")
                                        )
                                    ]
                                )
                            ],
                        )
                    )
                ]
            )
        )
        data = response.model_dump(by_alias=True, exclude_none=True)

        # Verify camelCase keys at every level
        nav = data["action"]["navigations"][0]
        assert "updateCard" in nav
        card = nav["updateCard"]
        assert card["header"]["imageUrl"] == "https://example.com/logo.png"
        assert card["header"]["imageType"] == "CIRCLE"
        assert card["sections"][0]["widgets"][0]["textParagraph"]["text"] == "Hello"

    def test_excludes_none_fields(self):
        """None fields should be excluded from serialized output."""
        response = CardResponse(
            action=ActionResponse(
                navigations=[
                    UpdateCard(
                        update_card=Card(
                            header=CardHeader(title="Test"),
                            sections=[
                                Section(
                                    widgets=[
                                        TextParagraphWidget(text_paragraph=TextParagraph(text="Hi"))
                                    ]
                                )
                            ],
                        )
                    )
                ]
            )
        )
        data = response.model_dump(by_alias=True, exclude_none=True)
        header = data["action"]["navigations"][0]["updateCard"]["header"]
        assert "imageUrl" not in header
        assert "subtitle" not in header
