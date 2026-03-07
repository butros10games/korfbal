"""Shared helpers for game tracker tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser
from django.utils import timezone

from apps.club.models import Club
from apps.game_tracker.models import GroupType, MatchData, MatchPart, PlayerGroup
from apps.player.models import Player
from apps.schedule.models import Match, Season
from apps.team.models import Team


TEST_PASSWORD = "testpass123"  # noqa: S105  # nosec B105 - test credential constant


@dataclass(frozen=True, slots=True)
class TrackerMatchContext:
    """Bundled match objects that most tracker tests need."""

    match: Match
    match_data: MatchData
    home_team: Team
    away_team: Team


def create_tracker_match(*, prefix: str) -> TrackerMatchContext:
    """Create a minimal match + MatchData fixture for tracker tests."""
    home_club = Club.objects.create(name=f"{prefix} Home Club")
    away_club = Club.objects.create(name=f"{prefix} Away Club")
    home_team = Team.objects.create(name=f"{prefix} Home Team", club=home_club)
    away_team = Team.objects.create(name=f"{prefix} Away Team", club=away_club)

    season = Season.objects.create(
        name=f"{prefix} Season",
        start_date=timezone.now().date() - timedelta(days=1),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=10),
    )

    return TrackerMatchContext(
        match=match,
        match_data=MatchData.objects.get(match_link=match),
        home_team=home_team,
        away_team=away_team,
    )


def create_group_types(*names: str) -> dict[str, GroupType]:
    """Create the requested tracker group types and return them by name."""
    return {name: GroupType.objects.create(name=name) for name in names}


def create_tracker_user(*, username: str) -> AbstractBaseUser:
    """Create a tracker test user."""
    user_model = cast(Any, get_user_model())
    return cast(
        AbstractBaseUser,
        user_model.objects.create_user(
            username=username,
            password=TEST_PASSWORD,
        ),
    )


def create_tracker_player(*, username: str) -> Player:
    """Create a tracker test player."""
    return cast(Player, cast(Any, create_tracker_user(username=username)).player)


def create_player_group(
    *,
    match_data: MatchData,
    team: Team,
    group_type: GroupType,
) -> PlayerGroup:
    """Create or return a PlayerGroup with matching starting/current type."""
    player_group, _ = PlayerGroup.objects.get_or_create(
        team=team,
        match_data=match_data,
        starting_type=group_type,
        defaults={"current_type": group_type},
    )
    if player_group.current_type_id != group_type.id_uuid:
        player_group.current_type = group_type
        player_group.save(update_fields=["current_type"])
    return player_group


def create_match_part(
    *,
    match_data: MatchData,
    part_number: int = 1,
    active: bool = True,
    start_offset: timedelta | None = None,
    end_offset: timedelta | None = None,
) -> MatchPart:
    """Create a match part with relative timestamps."""
    now = datetime.now(UTC)
    start_time = now + (start_offset or timedelta())
    end_time = None if end_offset is None else now + end_offset
    return MatchPart.objects.create(
        match_data=match_data,
        part_number=part_number,
        start_time=start_time,
        end_time=end_time,
        active=active,
    )
