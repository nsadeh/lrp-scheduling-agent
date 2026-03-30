"""Unit tests for Gmail message parsing."""

import base64

from api.gmail.models import (
    _extract_body_text,
    parse_email_address,
    parse_email_address_list,
    parse_message,
)


class TestParseEmailAddress:
    def test_simple_address(self):
        addr = parse_email_address("alice@example.com")
        assert addr.email == "alice@example.com"
        assert addr.name is None

    def test_display_name_and_address(self):
        addr = parse_email_address("Alice Smith <alice@example.com>")
        assert addr.email == "alice@example.com"
        assert addr.name == "Alice Smith"

    def test_quoted_display_name(self):
        addr = parse_email_address('"Smith, Alice" <alice@example.com>')
        assert addr.email == "alice@example.com"
        assert addr.name == "Smith, Alice"


class TestParseEmailAddressList:
    def test_multiple(self):
        addrs = parse_email_address_list("alice@ex.com, Bob <bob@ex.com>")
        assert len(addrs) == 2
        assert addrs[0].email == "alice@ex.com"
        assert addrs[1].email == "bob@ex.com"
        assert addrs[1].name == "Bob"

    def test_empty(self):
        assert parse_email_address_list("") == []


class TestExtractBodyText:
    def test_plain_text_direct(self):
        payload = {
            "mimeType": "text/plain",
            "body": {"data": base64.urlsafe_b64encode(b"Hello world").decode()},
        }
        assert _extract_body_text(payload) == "Hello world"

    def test_multipart_prefers_plain(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": base64.urlsafe_b64encode(b"Plain text").decode()},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": base64.urlsafe_b64encode(b"<b>HTML</b>").decode()},
                },
            ],
        }
        assert _extract_body_text(payload) == "Plain text"

    def test_html_fallback(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {
                        "data": base64.urlsafe_b64encode(b"<p>Hello <b>world</b></p>").decode()
                    },
                },
            ],
        }
        assert "Hello" in _extract_body_text(payload)
        assert "<p>" not in _extract_body_text(payload)

    def test_empty_payload(self):
        assert _extract_body_text({}) == ""


class TestParseMessage:
    def _make_raw_message(
        self,
        msg_id="msg123",
        thread_id="thr456",
        subject="Test Subject",
        from_addr="sender@example.com",
        to_addr="recipient@example.com",
        body_text="Hello",
        date="Mon, 30 Mar 2026 12:00:00 +0000",
    ):
        return {
            "id": msg_id,
            "threadId": thread_id,
            "snippet": "Hello...",
            "labelIds": ["INBOX"],
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Subject", "value": subject},
                    {"name": "From", "value": from_addr},
                    {"name": "To", "value": to_addr},
                    {"name": "Date", "value": date},
                    {"name": "Message-ID", "value": "<abc@example.com>"},
                ],
                "body": {"data": base64.urlsafe_b64encode(body_text.encode()).decode()},
            },
        }

    def test_basic_parse(self):
        raw = self._make_raw_message()
        msg = parse_message(raw)
        assert msg.id == "msg123"
        assert msg.thread_id == "thr456"
        assert msg.subject == "Test Subject"
        assert msg.from_.email == "sender@example.com"
        assert len(msg.to) == 1
        assert msg.to[0].email == "recipient@example.com"
        assert msg.body_text == "Hello"
        assert msg.snippet == "Hello..."
        assert msg.label_ids == ["INBOX"]
        assert msg.message_id_header == "<abc@example.com>"

    def test_date_parsing(self):
        raw = self._make_raw_message(date="Mon, 30 Mar 2026 12:00:00 +0000")
        msg = parse_message(raw)
        assert msg.date.year == 2026
        assert msg.date.month == 3
        assert msg.date.tzinfo is not None

    def test_display_name_from(self):
        raw = self._make_raw_message(from_addr="Alice Smith <alice@example.com>")
        msg = parse_message(raw)
        assert msg.from_.name == "Alice Smith"
        assert msg.from_.email == "alice@example.com"

    def test_missing_headers_safe(self):
        raw = {
            "id": "x",
            "threadId": "y",
            "payload": {"mimeType": "text/plain", "headers": [], "body": {}},
        }
        msg = parse_message(raw)
        assert msg.subject == ""
        assert msg.from_.email == ""
