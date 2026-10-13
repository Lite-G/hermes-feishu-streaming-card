"""Pure eligibility for reopening an expired approval review window.

The HTTP boundary must first verify the original session/interaction, callback
token and chat under its owner lock, then expire the interaction. A positive
result only permits showing a new window; it does not grant or restore consent.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, TYPE_CHECKING, cast, Mapping

if TYPE_CHECKING:
    from .session import CardSession, InteractionState


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


def runtime_admission_fingerprint(descriptor: dict[str, Any]) -> str:
    try:
        body = json.dumps(
            descriptor,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        return ""
    return hashlib.sha256(b"hfc-sidecar-runtime-admission-v1\0" + body).hexdigest()



@dataclass(frozen=True, repr=False)
class RuntimeCallbackSnapshot:
    """An in-flight request's original display owner, never an approval claim.

    Capture under the session lock. After network I/O, reacquire that same lock
    and compare against its current owner before applying the result. Referents
    deliberately retain object identity; field equality cannot revive an old
    window. Tokens/descriptors must never appear in automatic repr output.
    """

    session: CardSession
    interaction: InteractionState
    interaction_id: str
    callback_token: str
    chat_id: str
    fingerprint: str
    descriptor: dict[str, Any]
    choice: str
    turn_id: str

    def is_current(self, current_session: CardSession | None) -> bool:
        if current_session is None or current_session is not self.session:
            return False
        interaction = current_session.active_interaction
        return not (
            interaction is not self.interaction
            or interaction is None
            or interaction.interaction_id != self.interaction_id
            or interaction.callback_token != self.callback_token
            or current_session.chat_id != self.chat_id
            or interaction.status != "pending"
            or interaction.runtime_admission is None
            or runtime_admission_fingerprint(
                dict(cast(Mapping[str, Any], interaction.runtime_admission))
            ) != self.fingerprint
        )
