"""Explicit execution state transitions."""

from agent4_deployment_recovery.core.errors import InvalidTransitionError
from agent4_deployment_recovery.core.schemas import ExecutionState

ALLOWED_TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.RECEIVED: frozenset(
        {ExecutionState.AUTHORIZED, ExecutionState.REJECTED}
    ),
    ExecutionState.AUTHORIZED: frozenset(
        {
            ExecutionState.PREFLIGHT_PASSED,
            ExecutionState.REJECTED,
            ExecutionState.FAILED,
        }
    ),
    ExecutionState.PREFLIGHT_PASSED: frozenset(
        {ExecutionState.PLANNED, ExecutionState.FAILED}
    ),
    ExecutionState.PLANNED: frozenset(
        {ExecutionState.DRY_RUN_PASSED, ExecutionState.SUCCEEDED, ExecutionState.FAILED}
    ),
    ExecutionState.DRY_RUN_PASSED: frozenset(
        {ExecutionState.EXECUTING, ExecutionState.FAILED}
    ),
    ExecutionState.EXECUTING: frozenset(
        {
            ExecutionState.OUTCOME_UNCERTAIN,
            ExecutionState.OBSERVING,
            ExecutionState.ROLLBACK_PENDING,
            ExecutionState.FAILED,
        }
    ),
    ExecutionState.OUTCOME_UNCERTAIN: frozenset(
        {
            ExecutionState.OBSERVING,
            ExecutionState.ROLLBACK_PENDING,
            ExecutionState.FAILED,
        }
    ),
    ExecutionState.OBSERVING: frozenset(
        {
            ExecutionState.AWAITING_AGENT1,
            ExecutionState.ROLLBACK_PENDING,
            ExecutionState.FAILED,
        }
    ),
    ExecutionState.AWAITING_AGENT1: frozenset(
        {
            ExecutionState.SUCCEEDED,
            ExecutionState.VERIFICATION_INCONCLUSIVE,
            ExecutionState.ROLLBACK_PENDING,
        }
    ),
    ExecutionState.VERIFICATION_INCONCLUSIVE: frozenset(
        {ExecutionState.ROLLBACK_PENDING, ExecutionState.FAILED}
    ),
    ExecutionState.ROLLBACK_PENDING: frozenset(
        {ExecutionState.ROLLING_BACK, ExecutionState.ROLLBACK_FAILED}
    ),
    ExecutionState.ROLLING_BACK: frozenset(
        {ExecutionState.ROLLBACK_AWAITING_AGENT1, ExecutionState.ROLLBACK_FAILED}
    ),
    ExecutionState.ROLLBACK_AWAITING_AGENT1: frozenset(
        {ExecutionState.ROLLED_BACK, ExecutionState.ROLLBACK_FAILED}
    ),
    ExecutionState.SUCCEEDED: frozenset(),
    ExecutionState.REJECTED: frozenset(),
    ExecutionState.FAILED: frozenset(),
    ExecutionState.ROLLED_BACK: frozenset(),
    ExecutionState.ROLLBACK_FAILED: frozenset(),
}

TERMINAL_STATES = frozenset(
    {
        ExecutionState.SUCCEEDED,
        ExecutionState.REJECTED,
        ExecutionState.FAILED,
        ExecutionState.ROLLED_BACK,
        ExecutionState.ROLLBACK_FAILED,
    }
)


def validate_transition(current: ExecutionState, target: ExecutionState) -> None:
    allowed = ALLOWED_TRANSITIONS[current]
    if target not in allowed:
        allowed_text = ", ".join(sorted(state.value for state in allowed)) or "none"
        raise InvalidTransitionError(
            f"transition {current.value} -> {target.value} is not allowed; allowed: {allowed_text}"
        )


def is_terminal(state: ExecutionState) -> bool:
    return state in TERMINAL_STATES
