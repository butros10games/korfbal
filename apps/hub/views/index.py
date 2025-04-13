"""This module contains the view for the hub index page."""

from django.db.models import Q
from django.shortcuts import render

from apps.common.utils import get_time_display
from apps.game_tracker.models import MatchData, Shot
from apps.player.models import Player
from apps.schedule.models import Match
from apps.team.models import Team


def index(request):
    """View for the hub index page.

    Args:
        request (HttpRequest): The request object.

    Returns:
        HttpResponse: The response object

    """
    # get the players first upcoming match
    # get the player
    user_request = request.user
    if user_request.is_authenticated:
        player = Player.objects.get(user=user_request)
        # get the teams the player is connected to
        teams = Team.objects.filter(
            Q(team_data__players=player) | Q(team_data__coach=player)
        ).distinct()
        # get the matches of the teams
        matches = Match.objects.filter(
            Q(home_team__in=teams) | Q(away_team__in=teams)
        ).order_by("start_time")

        # get the match data of the matches
        match_data = (
            MatchData.objects.prefetch_related(
                "match_link", "match_link__home_team", "match_link__away_team"
            )
            .filter(match_link__in=matches, status__in=["active", "upcoming"])
            .order_by("match_link__start_time")
            .first()
        )

        match = None
        if match_data:
            match = match_data.match_link
    else:
        match = None
        match_data = None

    context = {
        "display_back": True,
        "match": match,
        "match_data": match_data,
        "match_date": (
            match.start_time.strftime("%a, %d %b") if match else "No upcoming matches"
        ),
        "start_time": match.start_time.strftime("%H:%M") if match else "",
        "time_display": get_time_display(match_data) if match_data else "",
        "home_score": (
            Shot.objects.filter(
                match_data=match_data, team=match.home_team, scored=True
            ).count()
            if match
            else 0
        ),
        "away_score": (
            Shot.objects.filter(
                match_data=match_data, team=match.away_team, scored=True
            ).count()
            if match
            else 0
        ),
    }

    return render(request, "hub/index.html", context)
