"""Outbound ports used by match-tracker application workflows."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from apps.game_tracker.realtime.contracts import LiveResource


if TYPE_CHECKING:
    from apps.schedule.models import Match, Season
    from apps.team.models import Team


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

    def match_started(self, *, match_id: str, match_data_id: str) -> None:
        """Schedule the match-start notification."""

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


class GoalAudioManifest(Protocol):
    """Resolve ready goal-song clips for the tracked roster (owned by player)."""

    def __call__(
        self, *, player_ids: Iterable[str], team: Team, season: Season | None
    ) -> dict[str, object]:
        """Return player and team-fallback clips in selection order."""


@dataclass(frozen=True, slots=True)
class TrackerRuntime:
    """Runtime capabilities required by tracker command execution."""

    now: Callable[[], datetime]
    jobs: TrackerJobDispatcher
    publisher: MatchChangePublisher
    goal_audio: GoalAudioManifest


class MatchForecaster(Protocol):
    """Predict a match outcome for public summaries (owned by competition)."""

    def __call__(self, match: Match) -> dict[str, Any]:
        """Return the public prediction payload for a native match."""


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


@dataclass(frozen=True, slots=True)
class PublicMatchReadRuntime:
    """Shared public read storage plus the forecast shown in match summaries."""

    store: PublishedLiveStore
    forecast: MatchForecaster
