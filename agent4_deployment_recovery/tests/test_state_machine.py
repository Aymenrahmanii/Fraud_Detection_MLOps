from itertools import pairwise

import pytest

from agent4_deployment_recovery.core.errors import InvalidTransitionError
from agent4_deployment_recovery.core.schemas import ExecutionState
from agent4_deployment_recovery.core.state_machine import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    is_terminal,
    validate_transition,
)


def test_happy_path_transitions_are_explicit():
    path = (
        ExecutionState.RECEIVED,
        ExecutionState.AUTHORIZED,
        ExecutionState.PREFLIGHT_PASSED,
        ExecutionState.PLANNED,
        ExecutionState.DRY_RUN_PASSED,
        ExecutionState.EXECUTING,
        ExecutionState.OBSERVING,
        ExecutionState.AWAITING_AGENT1,
        ExecutionState.SUCCEEDED,
    )
    for current, target in pairwise(path):
        validate_transition(current, target)
    assert is_terminal(ExecutionState.SUCCEEDED)


def test_invalid_transition_is_rejected():
    with pytest.raises(InvalidTransitionError):
        validate_transition(ExecutionState.RECEIVED, ExecutionState.EXECUTING)
    with pytest.raises(InvalidTransitionError):
        validate_transition(ExecutionState.SUCCEEDED, ExecutionState.ROLLING_BACK)


def test_rollback_path_is_explicit():
    validate_transition(ExecutionState.EXECUTING, ExecutionState.ROLLBACK_PENDING)
    validate_transition(ExecutionState.ROLLBACK_PENDING, ExecutionState.ROLLING_BACK)
    validate_transition(
        ExecutionState.ROLLING_BACK, ExecutionState.ROLLBACK_AWAITING_AGENT1
    )
    validate_transition(
        ExecutionState.ROLLBACK_AWAITING_AGENT1, ExecutionState.ROLLED_BACK
    )
    assert is_terminal(ExecutionState.ROLLED_BACK)


def test_every_terminal_state_has_no_outgoing_transition():
    assert TERMINAL_STATES
    for state in TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS[state] == frozenset()
