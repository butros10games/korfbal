"""Module contains the view for the hub index page."""

from typing import cast

from django.core.cache import cache
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from apps.game_tracker.models import MatchData, Shot
from apps.kwt_common.utils import get_time_display
from apps.player.models import Player
from apps.schedule.models import Match
from apps.team.models import Team


def _get_player_teams(player: Player) -> list[Team]:
    """Get teams that the player is associated with as player or coach.

    Args:
        player: The player to get teams for.

    Returns:
        List of teams the player is associated with.

    """
    cache_key = f"player_teams_{player.id_uuid}"
    cached_teams = cache.get(cache_key)

    if cached_teams is not None:
        return cast(list[Team], cached_teams)

    teams: list[Team] = list(
        Team.objects.select_related("club")
        .prefetch_related(
            "team_data__players",
            "team_data__coach",
            "team_data__season",
        )
        .filter(
            Q(team_data__players=player) | Q(team_data__coach=player),
        )
        .distinct()
    )

    # Cache for 5 minutes - teams don't change frequently
    cache.set(cache_key, teams, 300)

    return teams


def _get_upcoming_match_data(teams: list[Team]) -> MatchData | None:
    """Get the next upcoming or active match data for the given teams.

    Args:
        teams: List of teams to find matches for.

    Returns:
        The next upcoming or active match data, or None if no matches found.

    """
    if not teams:
        return None

    # First try to find matches where teams are playing at home
    match_data = (
        MatchData.objects.select_related(
            "match_link__home_team__club",
            "match_link__away_team__club",
            "match_link__season",
        )
        .prefetch_related(
            "match_link__home_team__team_data__players",
            "match_link__away_team__team_data__players",
        )
        .filter(match_link__home_team__in=teams, status__in=["active", "upcoming"])
        .order_by("match_link__start_time")
        .first()
    )

    # If no home matches found, check away matches
    if not match_data:
        match_data = (
            MatchData.objects.select_related(
                "match_link__home_team__club",
                "match_link__away_team__club",
                "match_link__season",
            )
            .prefetch_related(
                "match_link__home_team__team_data__players",
                "match_link__away_team__team_data__players",
            )
            .filter(match_link__away_team__in=teams, status__in=["active", "upcoming"])
            .order_by("match_link__start_time")
            .first()
        )

    return match_data


def _calculate_match_scores(match_data: MatchData) -> tuple[int, int]:
    """Calculate home and away scores for a match.

    Args:
        match_data: The match data to calculate scores for.

    Returns:
        Tuple of (home_score, away_score).

    """
    cache_key = f"match_scores_{match_data.id_uuid}"
    cached_scores = cache.get(cache_key)

    if cached_scores is not None:
        return cast(tuple[int, int], cached_scores)

    match = match_data.match_link
    scores: dict[str, int] = Shot.objects.filter(
        match_data=match_data, scored=True
    ).aggregate(
        home_score=Count("id_uuid", filter=Q(team=match.home_team)),
        away_score=Count("id_uuid", filter=Q(team=match.away_team)),
    )

    result: tuple[int, int] = (scores["home_score"] or 0, scores["away_score"] or 0)

    # Cache for 30 seconds - short cache for live match updates
    cache.set(cache_key, result, 30)

    return result


def _get_match_info(
    player: Player,
) -> tuple[Match | None, MatchData | None, int | None, int | None]:
    """Get match information for the player, including scores if applicable.

    Args:
        player: The player to get match info for.

    Returns:
        Tuple of (match, match_data, home_score, away_score).

    """
    teams = _get_player_teams(player)
    match_data = _get_upcoming_match_data(teams)

    if not match_data:
        return None, None, 0, 0

    match = match_data.match_link
    home_score: int | None
    away_score: int | None
    if match_data.status == "active":
        home_score, away_score = _calculate_match_scores(match_data)
    elif match_data.status == "upcoming":
        home_score = away_score = None
    else:
        home_score = away_score = 0

    return match, match_data, home_score, away_score


def index(request: HttpRequest) -> HttpResponse:
    """View for the hub index page.

    Args:
        request (HttpRequest): The request object.

    Returns:
        HttpResponse: The response object

    """
    match: Match | None = None
    match_data: MatchData | None = None
    home_score: int | None = 0
    away_score: int | None = 0

    if request.user.is_authenticated:
        try:
            player = Player.objects.get(user=request.user)
        except Player.DoesNotExist:
            player = None

        if player:
            match, match_data, home_score, away_score = _get_match_info(player)

    if match:
        match_date = match.start_time.strftime("%a, %d %b")
        start_time = match.start_time.strftime("%H:%M")
    else:
        match_date = "No upcoming matches"
        start_time = ""

    context = {
        "display_back": True,
        "match": match,
        "match_data": match_data,
        "match_date": match_date,
        "start_time": start_time,
        "time_display": get_time_display(match_data) if match_data else "",
        "home_score": home_score,
        "away_score": away_score,
    }

    return render(request, "hub/index.html", context)
