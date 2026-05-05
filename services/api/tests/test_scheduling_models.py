"""Tests for scheduling domain models."""

from datetime import UTC, datetime

from api.scheduling.models import (
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


class TestStatusBoard:
    def test_empty_board(self):
        board = StatusBoard()
        assert board.action_needed == []
        assert board.waiting == []
        assert board.scheduled == []
