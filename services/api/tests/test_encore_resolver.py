"""Tests for the deterministic Encore recruiter resolver.

Covers the 12 scenarios listed in rfcs/rfc-encore-recruiter-resolution.md
§Test Scenarios. The MS SQL layer is patched at `client.with_cursor` so
tests run without a live database; each test passes the row payloads the
resolver would have read from Encore.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pymssql
import pytest

from api.encore import client as encore_client
from api.encore.coordinators import (
    ADAM_EMAIL,
    ANA_EMAIL,
    FIONA_EMAIL,
    MARISSA_EMAIL,
)
from api.encore.models import (
    AmbiguousRecruiters,
    EncoreLookupError,
    NoMatch,
    Skipped,
    UniqueRecruiter,
)
from api.encore.resolver import resolve_recruiter

NOW = datetime(2026, 5, 20, tzinfo=UTC)


def _row(
    *,
    email: str,
    user_guid: str | None = None,
    time_entered: datetime | None = None,
    notes: str = "",
    genie_type: str = "General Information",
    display_name: str | None = None,
) -> dict:
    return {
        "UserEnteredGUID": user_guid or f"u_{email}",
        "TimeEntered": time_entered or NOW,
        "GenieNotes": notes,
        "GenieType": genie_type,
        "EmailAddress": email,
        "EmailDisplayName": display_name or email.split("@")[0].title(),
        "FirstName": "Daniel",
        "LastName": "Kim",
    }


@pytest.fixture(autouse=True)
def _reset_connection():
    encore_client._conn = None
    yield
    encore_client._conn = None


def _patch_rows(*row_batches: list[dict]):
    """Patch `client.with_cursor` so successive calls return successive batches.

    The resolver calls with_cursor once for the primary query, and a second
    time for the Ana initials lookup. Tests pass one batch for the primary
    only, or two batches when expecting the fallback to fire.
    """
    iterator = iter(row_batches)

    def fake_with_cursor(fn):
        # The lambda passed in calls queries.get_*; we ignore it and return
        # the next pre-baked batch directly.
        return next(iterator)

    return patch.object(encore_client, "with_cursor", side_effect=fake_with_cursor)


def _patch_raise(exc: Exception):
    def boom(_fn):
        raise exc

    return patch.object(encore_client, "with_cursor", side_effect=boom)


class TestResolverGates:
    @pytest.mark.asyncio
    async def test_adam_coordinator_skipped_without_sql_call(self):
        with patch.object(encore_client, "with_cursor") as spy:
            outcome = await resolve_recruiter("Daniel Kim", NOW, ADAM_EMAIL)
        assert isinstance(outcome, Skipped)
        assert outcome.reason == "coordinator_is_adam"
        spy.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["Daniel", "", "   "])
    async def test_single_word_name_skipped(self, name):
        with patch.object(encore_client, "with_cursor") as spy:
            outcome = await resolve_recruiter(name, NOW, FIONA_EMAIL)
        assert isinstance(outcome, Skipped)
        assert outcome.reason == "single_word_name"
        spy.assert_not_called()


class TestUniqueOutcome:
    @pytest.mark.asyncio
    async def test_unique_recent_recruiter(self):
        rows = [_row(email="rachel@lrp.com", user_guid="u_rachel")]
        with _patch_rows(rows):
            outcome = await resolve_recruiter("Daniel Kim", NOW, FIONA_EMAIL)
        assert isinstance(outcome, UniqueRecruiter)
        assert outcome.email == "rachel@lrp.com"
        assert outcome.source == "genie"

    @pytest.mark.asyncio
    async def test_unique_with_multiple_rows_same_recruiter(self):
        rows = [
            _row(email="rachel@lrp.com", user_guid="u_rachel", time_entered=NOW),
            _row(
                email="rachel@lrp.com",
                user_guid="u_rachel",
                time_entered=datetime(2025, 12, 1, tzinfo=UTC),
            ),
        ]
        with _patch_rows(rows):
            outcome = await resolve_recruiter("Daniel Kim", NOW, FIONA_EMAIL)
        assert isinstance(outcome, UniqueRecruiter)
        assert outcome.email == "rachel@lrp.com"


class TestAmbiguousOutcome:
    @pytest.mark.asyncio
    async def test_multiple_daniel_kims_returns_top_five_recency_sorted(self):
        rows = [
            _row(email=f"rec{i}@lrp.com", user_guid=f"u_{i}", time_entered=NOW.replace(month=i + 1))
            for i in range(6)
        ]
        with _patch_rows(rows):
            outcome = await resolve_recruiter("Daniel Kim", NOW, FIONA_EMAIL)
        assert isinstance(outcome, AmbiguousRecruiters)
        assert len(outcome.candidates) == 5
        # Most-recent first.
        assert outcome.candidates[0].email == "rec5@lrp.com"
        assert outcome.candidates[-1].email == "rec1@lrp.com"


class TestNoMatchOutcomes:
    @pytest.mark.asyncio
    async def test_candidate_not_in_tpeople(self):
        with _patch_rows([]):
            outcome = await resolve_recruiter("Phantom Person", NOW, FIONA_EMAIL)
        assert isinstance(outcome, NoMatch)
        assert outcome.reason == "no_genie_rows"

    @pytest.mark.asyncio
    async def test_all_genies_are_coordinator_authored_no_ana(self):
        rows = [
            _row(email=FIONA_EMAIL, user_guid="u_fiona"),
            _row(email=MARISSA_EMAIL, user_guid="u_marissa"),
        ]
        with _patch_rows(rows):
            outcome = await resolve_recruiter("Daniel Kim", NOW, FIONA_EMAIL)
        assert isinstance(outcome, NoMatch)
        assert outcome.reason == "no_non_coordinator"

    @pytest.mark.asyncio
    async def test_stale_only_genies_filtered_by_sql_cutoff(self):
        """If SQL returns 0 rows because the cutoff filtered everything, still NoMatch."""
        with _patch_rows([]):
            outcome = await resolve_recruiter("Daniel Kim", NOW, FIONA_EMAIL)
        assert isinstance(outcome, NoMatch)
        assert outcome.reason == "no_genie_rows"


class TestAnaFallback:
    @pytest.mark.asyncio
    async def test_ana_coord_parseable_initials_resolves(self):
        primary = [
            _row(
                email=ANA_EMAIL,
                user_guid="u_ana",
                notes="DC - submission for hedge fund role",
            )
        ]
        lookup = [{"EmailAddress": "dchen@lrp.com", "EmailDisplayName": "Dana Chen"}]
        with _patch_rows(primary, lookup):
            outcome = await resolve_recruiter("Daniel Kim", NOW, ANA_EMAIL)
        assert isinstance(outcome, UniqueRecruiter)
        assert outcome.email == "dchen@lrp.com"
        assert outcome.source == "ana_initials"

    @pytest.mark.asyncio
    async def test_ana_coord_unparseable_notes_no_match(self):
        primary = [_row(email=ANA_EMAIL, user_guid="u_ana", notes="follow-up call notes only")]
        with _patch_rows(primary), patch("api.encore.resolver.sentry_sdk") as sentry:
            outcome = await resolve_recruiter("Daniel Kim", NOW, ANA_EMAIL)
        assert isinstance(outcome, NoMatch)
        assert outcome.reason == "ana_parse_failed"
        sentry.capture_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_ana_in_genies_but_not_the_coordinator_no_fallback(self):
        primary = [
            _row(email=ANA_EMAIL, user_guid="u_ana", notes="DC - submission"),
        ]
        with _patch_rows(primary):
            outcome = await resolve_recruiter("Daniel Kim", NOW, FIONA_EMAIL)
        # Fiona is the coord, so Ana fallback should not fire even though
        # her rows are present and her notes look parseable.
        assert isinstance(outcome, NoMatch)
        assert outcome.reason == "no_non_coordinator"

    @pytest.mark.asyncio
    async def test_ana_initials_resolve_to_coordinator_rejected(self):
        primary = [
            _row(email=ANA_EMAIL, user_guid="u_ana", notes="AC - working this one myself"),
        ]
        # 'AC' lookup returns Ana's own email — guard must reject.
        lookup = [{"EmailAddress": ANA_EMAIL, "EmailDisplayName": "Ana Cooke"}]
        with _patch_rows(primary, lookup), patch("api.encore.resolver.sentry_sdk") as sentry:
            outcome = await resolve_recruiter("Daniel Kim", NOW, ANA_EMAIL)
        assert isinstance(outcome, NoMatch)
        assert outcome.reason == "resolved_to_coordinator"
        sentry.capture_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_ana_lookup_returns_zero_rows(self):
        primary = [
            _row(email=ANA_EMAIL, user_guid="u_ana", notes="XX - unknown initials"),
        ]
        lookup: list[dict] = []
        with _patch_rows(primary, lookup):
            outcome = await resolve_recruiter("Daniel Kim", NOW, ANA_EMAIL)
        assert isinstance(outcome, NoMatch)
        assert outcome.reason == "ana_lookup_empty"


class TestTailGuardOnPrimaryPath:
    @pytest.mark.asyncio
    async def test_primary_unique_email_is_a_coordinator_rejected(self):
        """Defense-in-depth: SQL filter missed a coordinator alias, tail guard fires."""
        # The resolver filters COORDINATOR_EMAILS itself, so the only way
        # this guard fires on the primary path is if a row passes Python
        # filtering but is then somehow a coordinator. Simulate by using an
        # alias email and patching COORDINATOR_EMAILS to include it AFTER
        # filtering — easier just to verify guard logic via the Ana branch,
        # which exercises the same `_guard_or_unique` helper. So we assert
        # the guard function explicitly here.
        from api.encore.resolver import _guard_or_unique

        with patch("api.encore.resolver.sentry_sdk") as sentry:
            outcome = _guard_or_unique(
                email=FIONA_EMAIL,
                display_name="Fiona Campbell",
                source="genie",
            )
        assert isinstance(outcome, NoMatch)
        assert outcome.reason == "resolved_to_coordinator"
        sentry.capture_message.assert_called_once()


class TestErrorOutcomes:
    @pytest.mark.asyncio
    async def test_mssql_operational_error(self):
        with (
            _patch_raise(pymssql.OperationalError("network down")),
            patch("api.encore.resolver.sentry_sdk") as sentry,
        ):
            outcome = await resolve_recruiter("Daniel Kim", NOW, FIONA_EMAIL)
        assert isinstance(outcome, EncoreLookupError)
        assert outcome.exception_type == "OperationalError"
        sentry.capture_exception.assert_called_once()

    @pytest.mark.asyncio
    async def test_mssql_database_error(self):
        with (
            _patch_raise(pymssql.DatabaseError("bad sql")),
            patch("api.encore.resolver.sentry_sdk"),
        ):
            outcome = await resolve_recruiter("Daniel Kim", NOW, FIONA_EMAIL)
        assert isinstance(outcome, EncoreLookupError)
        assert outcome.exception_type == "DatabaseError"


class TestPrefixMatching:
    @pytest.mark.asyncio
    async def test_dan_first_name_passes_full_name_with_percent(self):
        """The classifier-extracted name lands in the SQL params verbatim + '%'."""
        captured: dict = {}

        def fake_with_cursor(fn):
            # fn is the lambda that calls queries.get_recent_candidate_activity.
            # Substitute a fake cursor that captures the kwargs the query was called with.
            class FakeCur:
                def __init__(self):
                    self.captured = captured

            cur = FakeCur()
            from api.encore import queries as q_mod

            with patch.object(q_mod.queries, "get_recent_candidate_activity") as mock_q:
                mock_q.return_value = []
                fn(cur)
                captured.update(mock_q.call_args.kwargs)
            return []

        with patch.object(encore_client, "with_cursor", side_effect=fake_with_cursor):
            await resolve_recruiter("Dan Smith", NOW, FIONA_EMAIL)

        assert captured["last_name"] == "Smith"
        assert captured["first_name_like"] == "Dan%"
        # cutoff = NOW - 365 days
        assert (NOW - captured["cutoff"]).days == 365
