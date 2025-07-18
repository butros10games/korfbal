"""Module contains the view for the catalog page."""

from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from apps.player.models import Player
from apps.team.models import Team


def catalog(request: HttpRequest) -> HttpResponse:
    """View for the catalog page.

    Args:
        request (HttpRequest): The request object.

    Returns:
        HttpResponse: The response object.

    """
    connected_teams = None
    following_teams = None
    user = request.user
    if user.is_authenticated:
        # Get the Player object associated with this user
        player = Player.objects.get(user=user)

        connected_teams = Team.objects.filter(
            Q(team_data__players=player) | Q(team_data__coach=player),
        ).distinct()

        # Get all teams the user is following
        following_teams = player.team_follow.all()

        # remove the teams the user is part of from the teams the user is following
        following_teams = following_teams.exclude(id_uuid__in=connected_teams)

    context = {
        "connected": connected_teams,
        "following": following_teams,
        "display_back": True,
    }

    return render(request, "hub/catalog.html", context)
