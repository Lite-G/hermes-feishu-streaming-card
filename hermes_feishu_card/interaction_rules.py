"""Pure eligibility for reopening an expired approval review window.

The HTTP boundary must first verify the original session/interaction, callback
token and chat under its owner lock, then expire the interaction. A positive
result only permits showing a new window; it does not grant or restore consent.
"""
from __future__ import annotations

from dataclasses import dataclass


APPROVAL_RUNTIME_HEARTBEAT_SECONDS = 15.0


@dataclass(frozen=True)
class ApprovalWindowState:
    session_status: str
    interaction_status: str
    pause_on_timeout: bool
    has_runtime_admission: bool
    last_waiter_poll_at: float


def approval_window_can_reopen(state: ApprovalWindowState, *, now: float) -> bool:
    """Preserve the existing live-waiter and 15-second freshness contract."""
    if (
        state.session_status in {"completed", "failed"}
        or state.interaction_status != "paused"
        or not state.pause_on_timeout
        or state.has_runtime_admission
    ):
        return False
    return bool(
        state.last_waiter_poll_at
        and now - state.last_waiter_poll_at <= APPROVAL_RUNTIME_HEARTBEAT_SECONDS
    )
