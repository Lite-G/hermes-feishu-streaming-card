"""Delivery results shared by transports and orchestration, without I/O."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CardDeliveryResult:
    message_id: str | None
    outcome: str
    retry_count: int = 0
    error_kind: str = ""

    @property
    def delivered(self) -> bool:
        return self.outcome == "delivered" and bool(self.message_id)
