"""Shared helpers for game tracker tests."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser
from django.test.client import Client
from django.utils import timezone

from apps.club.models import Club
from apps.game_tracker.models import GroupType, MatchData, MatchPart, PlayerGroup
from apps.player.models import Player, PlayerClubMembership
from apps.schedule.models import Match, Season
from apps.team.models import Team


TEST_PASSWORD = "testpass123"  # nosec B105 - test credential constant
OnCommitCapture = Callable[
    ...,
    AbstractContextManager[list[Callable[[], None]]],
]


@dataclass(frozen=True, slots=True)
class TrackerMatchContext:
    """Bundled match objects that most tracker tests need."""

    match: Match
    match_data: MatchData
    home_team: Team
    away_team: Team


def create_tracker_match(
    *, prefix: str, start_offset: timedelta = -timedelta(minutes=10)
) -> TrackerMatchContext:
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
        start_time=timezone.now() + start_offset,
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


def create_tracker_user(*, username: str, email: str = "") -> AbstractBaseUser:
    """Create a tracker test user."""
    user_model = cast(Any, get_user_model())
    return cast(
        AbstractBaseUser,
        user_model.objects.create_user(
            username=username,
            email=email,
        ),
    )


def create_tracker_player(*, username: str, email: str = "") -> Player:
    """Create a tracker test player."""
    user = create_tracker_user(username=username, email=email)
    return cast(Player, cast(Any, user).player)


def connect_user_to_match_club(
    user: AbstractBaseUser,
    club: Club,
    match: Match,
) -> None:
    """Connect a user to a club before the match's local date."""
    PlayerClubMembership.objects.create(
        player=cast(Any, user).player,
        club=club,
        start_date=timezone.localdate(match.start_time) - timedelta(days=1),
    )


def login_home_club_editor(
    client: Client,
    tracker: TrackerMatchContext,
    username: str,
) -> AbstractBaseUser:
    """Create and log in an editor for a tracker's home club."""
    user = create_tracker_user(username=username)
    connect_user_to_match_club(user, tracker.home_team.club, tracker.match)
    client.force_login(user)
    return user


def get_tracker_group(
    tracker: TrackerMatchContext,
    name: str,
    team: Team | None = None,
) -> PlayerGroup:
    """Return a named group for the requested tracker team."""
    return PlayerGroup.objects.get(
        match_data=tracker.match_data,
        team=team or tracker.home_team,
        starting_type__name=name,
    )


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
