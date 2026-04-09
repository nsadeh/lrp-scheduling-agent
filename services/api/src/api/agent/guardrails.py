"""Output validation for agent results against scheduling state."""

from __future__ import annotations

from api.agent.models import AgentResult, DraftEmail, SuggestedAction
from api.scheduling.models import Loop, StageState

# Map suggested actions to the set of stage states where they are valid.
# An empty set means the action is only valid when NO loop exists.
VALID_ACTION_STATES: dict[SuggestedAction, set[StageState]] = {
    SuggestedAction.DRAFT_TO_RECRUITER: {StageState.NEW},
    SuggestedAction.DRAFT_TO_CLIENT: {StageState.AWAITING_CANDIDATE},
    SuggestedAction.DRAFT_CONFIRMATION: {StageState.AWAITING_CLIENT},
    SuggestedAction.DRAFT_FOLLOW_UP: {
        StageState.NEW,
        StageState.AWAITING_CANDIDATE,
        StageState.AWAITING_CLIENT,
    },
    SuggestedAction.REQUEST_NEW_AVAILABILITY: {StageState.AWAITING_CLIENT},
    SuggestedAction.MARK_COLD: {
        StageState.NEW,
        StageState.AWAITING_CANDIDATE,
        StageState.AWAITING_CLIENT,
        StageState.SCHEDULED,
    },
    SuggestedAction.CREATE_LOOP: set(),  # only valid when no loop exists
    SuggestedAction.ASK_COORDINATOR: {
        StageState.NEW,
        StageState.AWAITING_CANDIDATE,
        StageState.AWAITING_CLIENT,
        StageState.SCHEDULED,
    },
    SuggestedAction.NO_ACTION: {
        StageState.NEW,
        StageState.AWAITING_CANDIDATE,
        StageState.AWAITING_CLIENT,
        StageState.SCHEDULED,
        StageState.COMPLETE,
        StageState.COLD,
    },
}


def validate_action(result: AgentResult, loop: Loop | None) -> list[str]:
    """Validate the agent's suggested action against the current loop state.

    Returns a list of violation messages. An empty list means the action is
    valid.
    """
    violations: list[str] = []
    action = result.classification.suggested_action

    if action == SuggestedAction.CREATE_LOOP:
        if loop is not None:
            violations.append(f"create_loop suggested but a loop already exists (id={loop.id})")
        return violations

    # For all other actions, a loop should exist
    if loop is None:
        if action != SuggestedAction.NO_ACTION:
            violations.append(
                f"Action '{action.value}' requires an existing loop but none was found"
            )
        return violations

    valid_states = VALID_ACTION_STATES.get(action, set())
    if not valid_states:
        return violations

    # Check if any active stage is in a valid state for this action
    current_states = {stage.state for stage in loop.stages} if loop.stages else set()
    if not (current_states & valid_states):
        violations.append(
            f"Action '{action.value}' is not valid for current stage states "
            f"{sorted(s.value for s in current_states)}. "
            f"Valid states: {sorted(s.value for s in valid_states)}"
        )

    return violations


def validate_draft_recipients(draft: DraftEmail, known_emails: set[str]) -> list[str]:
    """Validate that draft recipients are known contacts.

    Returns a list of unknown recipient email addresses.
    """
    return [addr for addr in draft.to if addr not in known_emails]
