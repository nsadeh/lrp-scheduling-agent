"""Tests for scheduling domain models."""

from datetime import UTC, datetime

from api.scheduling.models import (
    ALLOWED_TRANSITIONS,
    Loop,
    Stage,
    StageState,
    StatusBoard,
)

NOW = datetime.now(UTC)


def _stage(state: StageState, name: str = "Round 1", ordinal: int = 0) -> Stage:
    return Stage(
        id="stg_test",
        loop_id="lop_test",
        name=name,
        state=state,
        ordinal=ordinal,
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

    def test_complete_is_terminal(self):
        assert ALLOWED_TRANSITIONS[StageState.COMPLETE] == set()

    def test_cold_can_revive_to_three_states(self):
        allowed = ALLOWED_TRANSITIONS[StageState.COLD]
        assert StageState.NEW in allowed
        assert StageState.AWAITING_CANDIDATE in allowed
        assert StageState.AWAITING_CLIENT in allowed

    def test_new_can_advance_or_go_cold(self):
        allowed = ALLOWED_TRANSITIONS[StageState.NEW]
        assert StageState.AWAITING_CANDIDATE in allowed
        assert StageState.AWAITING_CLIENT in allowed
        assert StageState.COLD in allowed
        assert len(allowed) == 3


class TestStage:
    def test_next_action_new(self):
        s = _stage(StageState.NEW)
        assert "recruiter" in s.next_action.lower()

    def test_is_active(self):
        assert _stage(StageState.NEW).is_active
        assert _stage(StageState.AWAITING_CANDIDATE).is_active
        assert not _stage(StageState.COMPLETE).is_active
        assert not _stage(StageState.COLD).is_active

    def test_is_actionable(self):
        assert _stage(StageState.NEW).is_actionable
        assert not _stage(StageState.AWAITING_CANDIDATE).is_actionable


class TestLoop:
    def _loop(self, stages: list[Stage]) -> Loop:
        return Loop(
            id="lop_test",
            coordinator_id="crd_test",
            client_contact_id="cli_test",
            recruiter_id="con_test",
            candidate_id="can_test",
            title="Test Loop",
            created_at=NOW,
            updated_at=NOW,
            stages=stages,
        )

    def test_computed_status_active(self):
        loop = self._loop([_stage(StageState.NEW)])
        assert loop.computed_status == "active"

    def test_computed_status_complete(self):
        loop = self._loop([_stage(StageState.COMPLETE)])
        assert loop.computed_status == "complete"

    def test_computed_status_cold(self):
        loop = self._loop([_stage(StageState.COLD)])
        assert loop.computed_status == "cold"

    def test_computed_status_all_scheduled(self):
        loop = self._loop(
            [
                _stage(StageState.SCHEDULED, "R1"),
                _stage(StageState.COMPLETE, "R2"),
            ]
        )
        assert loop.computed_status == "all_scheduled"

    def test_computed_status_empty(self):
        loop = self._loop([])
        assert loop.computed_status == "empty"

    def test_most_urgent_stage(self):
        s1 = _stage(StageState.AWAITING_CLIENT, "R1", 0)
        s2 = _stage(StageState.NEW, "R2", 1)
        loop = self._loop([s1, s2])
        # NEW has higher priority (lower number) than AWAITING_CLIENT
        assert loop.most_urgent_stage.name == "R2"

    def test_most_urgent_stage_none_when_all_done(self):
        loop = self._loop([_stage(StageState.COMPLETE)])
        assert loop.most_urgent_stage is None

    def test_active_stages(self):
        loop = self._loop(
            [
                _stage(StageState.NEW, "R1"),
                _stage(StageState.COMPLETE, "R2"),
                _stage(StageState.COLD, "R3"),
            ]
        )
        assert len(loop.active_stages) == 1
        assert loop.active_stages[0].name == "R1"


class TestStatusBoard:
    def test_empty_board(self):
        board = StatusBoard()
        assert board.action_needed == []
        assert board.waiting == []
        assert board.scheduled == []
