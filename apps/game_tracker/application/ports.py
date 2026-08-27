"""Outbound ports used by match-tracker application workflows."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apps.game_tracker.realtime.contracts import LiveResource


class MatchChangePublisher(Protocol):
    """Publish a committed tracker revision to realtime consumers."""

    def publish(
        self,
        *,
        match_id: str,
        revision: int,
        resources: Iterable[LiveResource | str],
    ) -> None:
        """Publish one committed match change."""


class TrackerJobDispatcher(Protocol):
    """Dispatch asynchronous work requested by tracker use cases."""

    def match_finished(self, *, match_id: str, match_data_id: str) -> None:
        """Schedule post-match notifications and publication."""

    def recompute_impacts(
        self,
        *,
        match_data_id: str,
        countdown_seconds: int = 0,
    ) -> None:
        """Schedule match-impact recomputation."""

    def recompute_minutes(
        self,
        *,
        match_data_id: str,
        countdown_seconds: int = 0,
    ) -> None:
        """Schedule minutes-played recomputation."""


@dataclass(frozen=True, slots=True)
class TrackerRuntime:
    """Runtime capabilities required by tracker command execution."""

    now: Callable[[], datetime]
    jobs: TrackerJobDispatcher
    publisher: MatchChangePublisher
