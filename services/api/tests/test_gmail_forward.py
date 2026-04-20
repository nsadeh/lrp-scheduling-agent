"""Unit tests for Gmail forward-body formatting (issue #36)."""

from datetime import UTC, datetime

from api.gmail.forward import (
    build_forwarded_body,
    prefix_forward_subject,
)
from api.gmail.models import EmailAddress, Message, Thread


def _msg(
    *,
    msg_id: str = "m1",
    thread_id: str = "t1",
    subject: str = "Phone screen for Claire Cao",
    from_name: str | None = "Alice Client",
    from_email: str = "alice@client.com",
    to: list[tuple[str | None, str]] | None = None,
    cc: list[tuple[str | None, str]] | None = None,
    date: datetime | None = None,
    body_text: str = "Hi — are you free Tuesday at 3pm?",
) -> Message:
    to = to or [("Coord", "coord@longridgepartners.com")]
    cc = cc or []
    return Message(
        id=msg_id,
        thread_id=thread_id,
        subject=subject,
        **{"from": EmailAddress(name=from_name, email=from_email)},
        to=[EmailAddress(name=n, email=e) for n, e in to],
        cc=[EmailAddress(name=n, email=e) for n, e in cc],
        date=date or datetime(2026, 4, 20, 9, 42, tzinfo=UTC),
        body_text=body_text,
    )


class TestBuildForwardedBody:
    def test_single_message_thread_formats_header_block(self):
        thread = Thread(id="t1", messages=[_msg()])
        result = build_forwarded_body("Please share availability.", thread)

        assert result.startswith("Please share availability.\n\n")
        assert "---------- Forwarded message ----------" in result
        assert "From: Alice Client <alice@client.com>" in result
        assert "Subject: Phone screen for Claire Cao" in result
        assert "To: Coord <coord@longridgepartners.com>" in result
        assert "Hi — are you free Tuesday at 3pm?" in result

    def test_multi_message_thread_chronological_with_separators(self):
        earlier = _msg(
            msg_id="m1",
            body_text="Original request body",
            date=datetime(2026, 4, 20, 9, 42, tzinfo=UTC),
        )
        later = _msg(
            msg_id="m2",
            body_text="Reply from coordinator",
            from_name="Coord",
            from_email="coord@longridgepartners.com",
            to=[("Alice Client", "alice@client.com")],
            date=datetime(2026, 4, 20, 10, 15, tzinfo=UTC),
        )
        thread = Thread(id="t1", messages=[earlier, later])
        result = build_forwarded_body("FYI", thread)

        # Order preserved: earlier body appears before later body.
        assert result.index("Original request body") < result.index("Reply from coordinator")
        # Two forward-header blocks.
        assert result.count("---------- Forwarded message ----------") == 2
        # Separator blank line between blocks.
        assert "Original request body\n\n---------- Forwarded message ----------" in result

    def test_note_preserved_on_top_with_blank_line_before_block(self):
        thread = Thread(id="t1", messages=[_msg()])
        result = build_forwarded_body("Short note.  \n\n", thread)

        # Note's trailing whitespace/newlines are trimmed, then exactly one blank line.
        assert result.startswith("Short note.\n\n---------- Forwarded message ----------")

    def test_omits_cc_line_when_empty(self):
        thread = Thread(id="t1", messages=[_msg(cc=[])])
        result = build_forwarded_body("", thread)
        assert "Cc:" not in result

    def test_includes_cc_line_when_present(self):
        thread = Thread(
            id="t1",
            messages=[_msg(cc=[("Bob", "bob@client.com"), (None, "carol@client.com")])],
        )
        result = build_forwarded_body("", thread)
        assert "Cc: Bob <bob@client.com>, carol@client.com" in result

    def test_empty_thread_returns_note_only(self):
        thread = Thread(id="t1", messages=[])
        assert build_forwarded_body("Just a note", thread) == "Just a note"

    def test_from_without_name_uses_bare_email(self):
        thread = Thread(id="t1", messages=[_msg(from_name=None)])
        result = build_forwarded_body("", thread)
        assert "From: alice@client.com" in result
        assert "<alice@client.com>" not in result


class TestPrefixForwardSubject:
    def test_adds_prefix_to_plain_subject(self):
        assert prefix_forward_subject("Phone screen") == "Fwd: Phone screen"

    def test_idempotent_for_fwd_prefix(self):
        assert prefix_forward_subject("Fwd: Phone screen") == "Fwd: Phone screen"

    def test_case_insensitive_dedupe(self):
        assert prefix_forward_subject("FWD: phone") == "FWD: phone"
        assert prefix_forward_subject("fwd: phone") == "fwd: phone"

    def test_leading_whitespace_still_dedupes(self):
        assert prefix_forward_subject("   Fwd: phone") == "   Fwd: phone"

    def test_re_prefix_is_not_treated_as_forward(self):
        assert prefix_forward_subject("Re: Phone screen") == "Fwd: Re: Phone screen"
