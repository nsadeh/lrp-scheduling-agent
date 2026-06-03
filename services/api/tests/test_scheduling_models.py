"""Tests for scheduling domain models."""

from datetime import UTC, datetime

from api.scheduling.models import (
    ClientContact,
    Loop,
    StageState,
    StatusBoard,
)

NOW = datetime.now(UTC)


def _loop(state: StageState = StageState.NEW) -> Loop:
    return Loop(
        id="lop_test",
        coordinator_id="crd_test",
        client_contact_id="cli_test",
        recruiter_id="con_test",
        candidate_id="can_test",
        title="Test Loop",
        state=state,
        created_at=NOW,
        updated_at=NOW,
    )


class TestStageState:
    def test_all_states_have_next_action(self):
        from api.scheduling.models import NEXT_ACTIONS

        for state in StageState:
            assert state in NEXT_ACTIONS

    def test_all_states_have_priority(self):
        from api.scheduling.models import STATE_PRIORITY

        for state in StageState:
            assert state in STATE_PRIORITY


class TestLoop:
    def test_next_action_new(self):
        loop = _loop(StageState.NEW)
        assert "recruiter" in loop.next_action.lower()

    def test_is_active(self):
        assert _loop(StageState.NEW).is_active
        assert _loop(StageState.AWAITING_CANDIDATE).is_active
        assert not _loop(StageState.COMPLETE).is_active
        assert not _loop(StageState.COLD).is_active

    def test_is_actionable(self):
        assert _loop(StageState.NEW).is_actionable
        assert not _loop(StageState.AWAITING_CANDIDATE).is_actionable


class TestLoopClientCompanyDisplay:
    @staticmethod
    def _contact(company: str | None) -> ClientContact:
        return ClientContact(
            id="cli_1",
            name="Jane Doe",
            email="jane@example.com",
            company=company,
            created_at=NOW,
        )

    def _loop_with(self, title: str, contact: ClientContact | None) -> Loop:
        loop = _loop()
        loop.title = title
        loop.client_contact = contact
        return loop

    def test_prefers_client_contact_company(self):
        loop = self._loop_with("John Doe, Wrong Inc", self._contact("Big Bank"))
        assert loop.client_company_display == "Big Bank"

    def test_falls_back_to_title_when_no_contact(self):
        """ATS-created loop case: contact is None but company is in title."""
        loop = self._loop_with("John Doe, Big Bank", None)
        assert loop.client_company_display == "Big Bank"

    def test_falls_back_to_title_when_contact_has_no_company(self):
        loop = self._loop_with("John Doe, Big Bank", self._contact(None))
        assert loop.client_company_display == "Big Bank"

    def test_bare_candidate_name_title_returns_none(self):
        loop = self._loop_with("John Doe", None)
        assert loop.client_company_display is None

    def test_handles_candidate_name_with_comma(self):
        """rsplit-from-right correctly handles "Smith, Jr., Big Bank"."""
        loop = self._loop_with("Smith, Jr., Big Bank", None)
        assert loop.client_company_display == "Big Bank"

    def test_empty_company_segment_returns_none(self):
        loop = self._loop_with("John Doe, ", None)
        assert loop.client_company_display is None


class TestStatusBoard:
    def test_empty_board(self):
        board = StatusBoard()
        assert board.action_needed == []
        assert board.waiting == []
        assert board.scheduled == []
