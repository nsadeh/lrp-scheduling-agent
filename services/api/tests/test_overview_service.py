"""Tests for the overview service — grouping logic (no DB needed)."""

from datetime import UTC, datetime

from api.classifier.models import (
    EmailClassification,
    SuggestedAction,
    Suggestion,
    SuggestionStatus,
)
from api.overview.models import SuggestionView
from api.overview.service import group_by_loop


def _suggestion(
    suggestion_id: str = "sug_1",
    loop_id: str | None = "lop_1",
    action: SuggestedAction = SuggestedAction.ADVANCE_STAGE,
    created_at: datetime | None = None,
) -> Suggestion:
    return Suggestion(
        id=suggestion_id,
        coordinator_email="fiona@lrp.com",
        gmail_message_id="msg_1",
        gmail_thread_id="thread_1",
        loop_id=loop_id,
        stage_id="stg_1",
        classification=EmailClassification.AVAILABILITY_RESPONSE,
        action=action,
        confidence=0.9,
        summary="Test suggestion",
        status=SuggestionStatus.PENDING,
        created_at=created_at or datetime(2026, 4, 15, 10, 0, tzinfo=UTC),
    )


def _view(
    suggestion_id: str = "sug_1",
    loop_id: str | None = "lop_1",
    loop_title: str | None = "Jane Doe, ACME",
    created_at: datetime | None = None,
    action: SuggestedAction = SuggestedAction.ADVANCE_STAGE,
) -> SuggestionView:
    return SuggestionView(
        suggestion=_suggestion(
            suggestion_id=suggestion_id,
            loop_id=loop_id,
            action=action,
            created_at=created_at,
        ),
        loop_title=loop_title,
        candidate_name="Jane Doe",
        client_company="ACME",
    )


class TestGroupByLoop:
    def test_empty_list_returns_empty(self):
        assert group_by_loop([]) == []

    def test_single_suggestion_single_group(self):
        groups = group_by_loop([_view()])
        assert len(groups) == 1
        assert groups[0].loop_id == "lop_1"
        assert len(groups[0].suggestions) == 1

    def test_same_loop_groups_together(self):
        views = [
            _view(suggestion_id="sug_1", loop_id="lop_1"),
            _view(suggestion_id="sug_2", loop_id="lop_1"),
        ]
        groups = group_by_loop(views)
        assert len(groups) == 1
        assert len(groups[0].suggestions) == 2

    def test_different_loops_separate_groups(self):
        views = [
            _view(suggestion_id="sug_1", loop_id="lop_1"),
            _view(suggestion_id="sug_2", loop_id="lop_2", loop_title="Bob, XYZ"),
        ]
        groups = group_by_loop(views)
        assert len(groups) == 2

    def test_none_loop_id_groups_separately(self):
        views = [
            _view(suggestion_id="sug_1", loop_id=None, loop_title=None),
            _view(suggestion_id="sug_2", loop_id="lop_1"),
        ]
        groups = group_by_loop(views)
        assert len(groups) == 2

    def test_sorted_by_oldest_created_at(self):
        views = [
            _view(
                suggestion_id="sug_2",
                loop_id="lop_2",
                loop_title="Later",
                created_at=datetime(2026, 4, 16, tzinfo=UTC),
            ),
            _view(
                suggestion_id="sug_1",
                loop_id="lop_1",
                loop_title="Earlier",
                created_at=datetime(2026, 4, 14, tzinfo=UTC),
            ),
        ]
        groups = group_by_loop(views)
        assert groups[0].loop_title == "Earlier"
        assert groups[1].loop_title == "Later"

    def test_group_inherits_loop_metadata(self):
        views = [
            _view(suggestion_id="sug_1", loop_id="lop_1", loop_title="Jane, ACME"),
        ]
        groups = group_by_loop(views)
        assert groups[0].loop_title == "Jane, ACME"
        assert groups[0].candidate_name == "Jane Doe"
        assert groups[0].client_company == "ACME"

    def test_create_loop_no_loop_id(self):
        """CREATE_LOOP suggestions typically have no loop_id."""
        views = [
            _view(
                suggestion_id="sug_1",
                loop_id=None,
                loop_title=None,
                action=SuggestedAction.CREATE_LOOP,
            ),
        ]
        groups = group_by_loop(views)
        assert len(groups) == 1
        assert groups[0].loop_id is None
