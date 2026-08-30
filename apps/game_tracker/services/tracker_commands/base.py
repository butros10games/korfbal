"""Shared tracker command contracts and match-state guards."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast

from apps.game_tracker.application.ports import TrackerJobDispatcher
from apps.game_tracker.models import MatchData, MatchPart, Pause
from apps.schedule.models import Match
from apps.team.models.team import Team


MATCH_IS_PAUSED_MESSAGE = "match is paused"
NO_ACTIVE_MATCH_PART_MESSAGE = "No active match part."


class TrackerCommandError(RuntimeError):
    """Raised when a tracker command cannot be applied."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "error",
        details: dict[str, object] | None = None,
    ) -> None:
        """Create an error that can be mapped to an API response."""
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class TrackerCommandContext:
    """Locked match state and runtime capabilities supplied to a command."""

    match: Match
    match_data: MatchData
    team: Team
    event_time: datetime
    jobs: TrackerJobDispatcher


class TrackerCommand(Protocol):
    """A parsed command that can mutate locked tracker state."""

    def apply(self, context: TrackerCommandContext) -> None:
        """Apply the command against the locked tracker state."""


def other_team(match: Match, team: Team) -> Team:
    """Return the participating team opposite the reporting team.

    Raises:
        TrackerCommandError: If the reporting team does not participate.

    """
    if cast(Any, match).home_team_id == team.id_uuid:
        return match.away_team
    if cast(Any, match).away_team_id == team.id_uuid:
        return match.home_team
    raise TrackerCommandError(
        "Team is not participating in this match.",
        code="invalid_team",
    )


def current_part(match_data: MatchData) -> MatchPart | None:
    """Return the newest active match part."""
    return (
        MatchPart.objects
        .filter(match_data=match_data, active=True)
        .order_by("-start_time", "-id_uuid")
        .first()
    )


def is_paused(match_data: MatchData, match_part: MatchPart | None) -> bool:
    """Return whether tracker mutations requiring live play must be blocked."""
    if match_data.status != "active" or match_part is None:
        return True
    return Pause.objects.filter(
        match_data=match_data,
        active=True,
        match_part=match_part,
    ).exists()


def require_live_part(
    match_data: MatchData,
    team: Team,
    match: Match,
) -> tuple[MatchPart, Team]:
    """Return the active part and opponent.

    Raises:
        TrackerCommandError: If the match is paused or has no active part.

    """
    match_part = current_part(match_data)
    opponent = other_team(match, team)
    if match_part is None:
        raise TrackerCommandError(
            NO_ACTIVE_MATCH_PART_MESSAGE,
            code="no_active_part",
        )
    if is_paused(match_data, match_part):
        raise TrackerCommandError(MATCH_IS_PAUSED_MESSAGE, code="match_paused")
    return match_part, opponent
