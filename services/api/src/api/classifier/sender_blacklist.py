"""Sender blacklist — short-circuits the classifier for known non-client senders.

Loaded once at app startup from sender_blacklist.yaml. The check runs in
ClassifierHook.on_email() before any LLM work, only for incoming emails on
threads that are not linked to a scheduling loop.

See sender_blacklist.yaml for the seed list and the rationale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).parent / "sender_blacklist.yaml"


@dataclass(frozen=True)
class SenderBlacklist:
    """Immutable deny-list keyed on bare email address.

    Both fields hold lowercased strings. ``domains`` is matched against the
    portion after the last ``@``; ``emails`` is matched against the full
    address. Use the ``empty()`` classmethod for tests / disabled state.
    """

    domains: frozenset[str] = field(default_factory=frozenset)
    emails: frozenset[str] = field(default_factory=frozenset)

    def is_blocked(self, email: str | None) -> bool:
        if not email:
            return False
        addr = email.strip().lower()
        if not addr:
            return False
        if addr in self.emails:
            return True
        _, _, domain = addr.rpartition("@")
        return bool(domain) and domain in self.domains

    @classmethod
    def empty(cls) -> SenderBlacklist:
        return cls()


def load_blacklist(path: Path = DEFAULT_PATH) -> SenderBlacklist:
    """Load and normalize the YAML blacklist file.

    Missing file → empty blacklist + WARNING log (fail-open: a typo in the
    deploy shouldn't take down the classifier). Malformed YAML or wrong
    schema → empty blacklist + WARNING log; we never raise.
    """
    if not path.exists():
        logger.warning("sender_blacklist.yaml not found at %s — blacklist disabled", path)
        return SenderBlacklist.empty()

    try:
        raw: Any = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        logger.exception("failed to parse sender_blacklist.yaml at %s — blacklist disabled", path)
        return SenderBlacklist.empty()

    if not isinstance(raw, dict):
        logger.warning(
            "sender_blacklist.yaml at %s has unexpected top-level type %s — blacklist disabled",
            path,
            type(raw).__name__,
        )
        return SenderBlacklist.empty()

    domains = frozenset(
        d.strip().lower() for d in (raw.get("domains") or []) if isinstance(d, str) and d.strip()
    )
    emails = frozenset(
        e.strip().lower() for e in (raw.get("emails") or []) if isinstance(e, str) and e.strip()
    )

    logger.info(
        "sender blacklist loaded from %s: %d domains, %d emails", path, len(domains), len(emails)
    )
    return SenderBlacklist(domains=domains, emails=emails)
