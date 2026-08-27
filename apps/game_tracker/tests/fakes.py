"""In-memory adapters for match-tracker application tests."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from apps.game_tracker.realtime.contracts import LiveResource


@dataclass(frozen=True, slots=True)
class PublishedMatchChange:
    """One recorded realtime publication."""

    match_id: str
    revision: int
    resources: frozenset[str]


@dataclass(slots=True)
class RecordingMatchChangePublisher:
    """Capture realtime publications without Channels or Valkey."""

    changes: list[PublishedMatchChange] = field(default_factory=list)

    def publish(
        self,
        *,
        match_id: str,
        revision: int,
        resources: Iterable[LiveResource | str],
    ) -> None:
        """Record one publication."""
        self.changes.append(
            PublishedMatchChange(
                match_id=match_id,
                revision=revision,
                resources=frozenset(str(resource) for resource in resources),
            )
        )
