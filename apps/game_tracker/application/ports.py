"""Outbound ports used by match-tracker application workflows."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from apps.game_tracker.realtime.contracts import LiveResource


class MatchChangePublisher(Protocol):
    """Publish a committed tracker revision to realtime consumers."""

    def schedule_snapshot(self, *, match_id: str, revision: int) -> None:
        """Persist snapshot publication intent inside the mutation transaction."""

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


class PublicLiveStoreError(Exception):
    """Shared snapshot storage is temporarily unavailable."""


class PublishedLiveStore(Protocol):
    """Latest committed public state with monotonic revision publication."""

    def get(self, match_id: str) -> dict[str, Any] | None:
        """Return a fresh shared envelope, or request authoritative recovery."""

    def put(self, match_id: str, envelope: dict[str, Any]) -> None:
        """Publish unless a newer revision or deletion fence already exists."""

    def invalidate(self, match_id: str, revision: int) -> None:
        """Fence older snapshots after a committed mutation."""

    def recover(
        self,
        match_id: str,
        minimum_revision: int,
        build: Callable[[], dict[str, Any] | None],
    ) -> dict[str, Any] | None:
        """Coalesce misses briefly, falling back to the authoritative builder."""
