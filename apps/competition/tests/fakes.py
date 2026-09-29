"""Capability fakes for competition application services."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RecordingScheduleChanges:
    """Record official schedule-change dispatches instead of queueing jobs."""

    calls: list[dict[str, object]] = field(default_factory=list)

    def __call__(
        self, *, notification_id: str, match_id: str, starts_at: str, cancelled: bool
    ) -> None:
        """Retain the dispatched publication for assertions."""
        self.calls.append({
            "notification_id": notification_id,
            "match_id": match_id,
            "starts_at": starts_at,
            "cancelled": cancelled,
        })
