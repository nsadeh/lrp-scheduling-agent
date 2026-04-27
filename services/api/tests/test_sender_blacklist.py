"""Tests for SenderBlacklist — domain + email matching, YAML loader."""

from pathlib import Path

import pytest

from api.classifier.sender_blacklist import SenderBlacklist, load_blacklist


def test_empty_blacklist_blocks_nothing():
    bl = SenderBlacklist.empty()
    assert bl.is_blocked("anyone@anywhere.com") is False
    assert bl.is_blocked("") is False
    assert bl.is_blocked(None) is False


def test_domain_match_blocks_any_localpart():
    bl = SenderBlacklist(domains=frozenset({"news.pitchbook.com"}))
    assert bl.is_blocked("news-noreply@news.pitchbook.com") is True
    assert bl.is_blocked("anyone@news.pitchbook.com") is True


def test_domain_match_does_not_block_subdomains_or_parents():
    # Explicit subdomains only — listing pitchbook.com should NOT block
    # news.pitchbook.com, and listing news.pitchbook.com should NOT block
    # pitchbook.com. This is the intentional "no suffix matching" behavior.
    bl = SenderBlacklist(domains=frozenset({"pitchbook.com"}))
    assert bl.is_blocked("anyone@news.pitchbook.com") is False
    assert bl.is_blocked("anyone@pitchbook.com") is True


def test_exact_email_match():
    bl = SenderBlacklist(emails=frozenset({"karenleone707@gmail.com"}))
    assert bl.is_blocked("karenleone707@gmail.com") is True
    # Same domain, different localpart — not blocked
    assert bl.is_blocked("someone-else@gmail.com") is False


def test_match_is_case_insensitive():
    bl = SenderBlacklist(
        domains=frozenset({"linkedin.com"}),
        emails=frozenset({"karen@gmail.com"}),
    )
    assert bl.is_blocked("Foo@LINKEDIN.com") is True
    assert bl.is_blocked("KAREN@GMAIL.COM") is True


def test_match_strips_whitespace():
    bl = SenderBlacklist(domains=frozenset({"linkedin.com"}))
    assert bl.is_blocked("  noreply@linkedin.com  ") is True


def test_load_blacklist_parses_yaml(tmp_path: Path):
    yaml_path = tmp_path / "test_blacklist.yaml"
    yaml_path.write_text(
        """
domains:
  - linkedin.com
  - News.PitchBook.com   # case is normalized
  - ""                     # blank entries dropped
  -                        # null entries dropped

emails:
  - karen@gmail.com
"""
    )
    bl = load_blacklist(yaml_path)
    assert "linkedin.com" in bl.domains
    assert "news.pitchbook.com" in bl.domains
    assert "" not in bl.domains
    assert "karen@gmail.com" in bl.emails
    # Sanity end-to-end
    assert bl.is_blocked("noreply@linkedin.com") is True


def test_load_blacklist_missing_file_returns_empty(tmp_path: Path):
    bl = load_blacklist(tmp_path / "does-not-exist.yaml")
    assert bl == SenderBlacklist.empty()


def test_load_blacklist_malformed_yaml_returns_empty(tmp_path: Path):
    yaml_path = tmp_path / "bad.yaml"
    yaml_path.write_text("not: [valid: yaml: at all")
    bl = load_blacklist(yaml_path)
    assert bl == SenderBlacklist.empty()


def test_load_blacklist_wrong_top_level_returns_empty(tmp_path: Path):
    # YAML that parses but isn't a dict at top level
    yaml_path = tmp_path / "list.yaml"
    yaml_path.write_text("- just\n- a\n- list\n")
    bl = load_blacklist(yaml_path)
    assert bl == SenderBlacklist.empty()


def test_load_blacklist_empty_file_returns_empty(tmp_path: Path):
    yaml_path = tmp_path / "empty.yaml"
    yaml_path.write_text("")
    bl = load_blacklist(yaml_path)
    assert bl == SenderBlacklist.empty()


def test_load_default_yaml_is_well_formed():
    """The shipped YAML at api/classifier/sender_blacklist.yaml must load cleanly."""
    bl = load_blacklist()
    # Sanity: at least the obvious newsletter senders we audited are present
    assert bl.is_blocked("alerts@withintelligence-email.com") is True
    assert bl.is_blocked("news-noreply@news.pitchbook.com") is True
    assert bl.is_blocked("messages-noreply@linkedin.com") is True
    assert bl.is_blocked("no-reply@zoom.us") is True
    # Sanity: real candidate domains aren't blocked
    assert bl.is_blocked("alice@example.com") is False
    assert bl.is_blocked("recruiter@longridgepartners.com") is False


@pytest.mark.parametrize(
    "addr",
    ["", "   ", "no-at-sign", "@no-localpart.com"],
)
def test_is_blocked_handles_malformed_addresses(addr: str):
    bl = SenderBlacklist(domains=frozenset({"linkedin.com"}))
    # None of these should ever raise — gracefully return False
    bl.is_blocked(addr)
