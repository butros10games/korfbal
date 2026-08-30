"""Setup helpers for schedule match API tests."""

from datetime import datetime, timedelta
from typing import Any, NamedTuple, cast

from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser
from django.test.client import Client
from django.utils import timezone

from apps.club.models import Club
from apps.game_tracker.models import GoalType, MatchData, MatchPart, MatchPlayer
from apps.player.models import Player
from apps.schedule.models import Match, Season
from apps.team.models import Team
from apps.team.models.team_data import TeamData


class MatchGraph(NamedTuple):
    """Minimal persisted match graph."""

    match: Match
    match_data: MatchData
    home_team: Team
    away_team: Team


class EditorContext(NamedTuple):
    """Authenticated event editor context."""

    graph: MatchGraph
    match_part: MatchPart
    actor: Player


def create_user(*, username: str) -> AbstractBaseUser:
    """Create a typed user."""
    model = cast(Any, get_user_model())
    return cast(AbstractBaseUser, model.objects.create_user(username=username))


def create_match_graph(
    *, prefix: str, start_time: datetime | None = None
) -> MatchGraph:
    """Create a match graph around the supplied local date."""
    match_start = start_time or timezone.now()
    match_date = timezone.localdate(match_start)
    home = Team.objects.create(
        name=f"{prefix} Home", club=Club.objects.create(name=f"{prefix} HC")
    )
    away = Team.objects.create(
        name=f"{prefix} Away", club=Club.objects.create(name=f"{prefix} AC")
    )
    season = Season.objects.create(
        name=f"{prefix} Season",
        start_date=match_date - timedelta(days=1),
        end_date=match_date + timedelta(days=1),
    )
    match = Match.objects.create(
        home_team=home, away_team=away, season=season, start_time=match_start
    )
    return MatchGraph(match, MatchData.objects.get(match_link=match), home, away)


def create_match_part(graph: MatchGraph) -> MatchPart:
    """Create the active opening period."""
    return MatchPart.objects.create(
        match_data=graph.match_data,
        part_number=1,
        start_time=timezone.now() - timedelta(minutes=1),
        active=True,
    )


def assign_coach(
    graph: MatchGraph, user: AbstractBaseUser, *, team: Team | None = None
) -> Player:
    """Assign a participating team's coach."""
    player = cast(Player, cast(Any, user).player)
    team_data, _ = TeamData.objects.get_or_create(
        team=team or graph.home_team, season=graph.match.season
    )
    team_data.coach.add(player)
    return player


def login_coach(client: Client, graph: MatchGraph, *, username: str) -> Player:
    """Create and authenticate the home coach."""
    player = assign_coach(graph, user := create_user(username=username))
    client.force_login(user)
    return player


def add_roster_player(
    graph: MatchGraph, user: AbstractBaseUser, *, team: Team
) -> Player:
    """Add a user's player to one match team."""
    player = cast(Player, cast(Any, user).player)
    MatchPlayer.objects.create(match_data=graph.match_data, team=team, player=player)
    return player


def create_editor_context(client: Client, *, username: str) -> EditorContext:
    """Create an authenticated home-coach editor."""
    graph = create_match_graph(prefix=username)
    actor = assign_coach(graph, user := create_user(username=username))
    add_roster_player(graph, user, team=graph.home_team)
    client.force_login(user)
    return EditorContext(graph, create_match_part(graph), actor)


def goal_payload(
    context: EditorContext,
    goal_type: GoalType | None = None,
    *,
    expected_revision: int | None,
    **overrides: object,
) -> dict[str, object]:
    """Build a valid goal payload with optional overrides."""
    goal_type = goal_type or GoalType.objects.create(name="Test goal")
    if expected_revision is not None:
        overrides["expected_revision"] = expected_revision
    return {
        "player_id": str(context.actor.id_uuid),
        "team_id": str(context.graph.home_team.id_uuid),
        "shot_type_id": str(goal_type.id_uuid),
        "match_part_id": str(context.match_part.id_uuid),
        "minute": 0,
        **overrides,
    }
