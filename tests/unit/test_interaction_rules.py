"""Approval renewal eligibility preserves the live-waiter cutoff without consent."""
from dataclasses import replace

import pytest

from hermes_feishu_card.interaction_rules import ApprovalWindowState, approval_window_can_reopen


def window(**changes):
    return replace(ApprovalWindowState("thinking", "paused", True, False, 90.0), **changes)


@pytest.mark.parametrize("last_poll,expected", [(85.001, True), (85.0, True), (84.999, False), (0.0, False), (101.0, True)])
def test_waiter_freshness_keeps_inclusive_15_second_boundary(last_poll, expected):
    # A future timestamp retains the established behavior of the old predicate;
    # this source move must not silently add a separate clock policy.
    state = window(last_waiter_poll_at=last_poll)
    assert approval_window_can_reopen(state, now=100.0) is expected
    assert state.last_waiter_poll_at == last_poll


@pytest.mark.parametrize("changes", [
    {"session_status": "completed"}, {"session_status": "failed"},
    {"interaction_status": "pending"}, {"interaction_status": "completed"},
    {"interaction_status": "failed"}, {"pause_on_timeout": False},
    {"has_runtime_admission": True},
])
def test_other_owners_or_closed_windows_cannot_be_reopened_even_with_fresh_waiter(changes):
    assert not approval_window_can_reopen(window(**changes), now=100.0)


def test_reopen_eligibility_is_only_a_read_only_decision_for_the_same_window():
    state = window(session_status="tool_running")
    before = replace(state)
    assert approval_window_can_reopen(state, now=100.0)
    assert state == before
