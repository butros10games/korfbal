"""Module contains the view for the team detail page."""

from datetime import date

from django.http import Http404
from django.shortcuts import get_object_or_404, render

from apps.player.models import Player
from apps.schedule.models import Season
from apps.team.models import Team, TeamData


def team_detail(request, team_id):
    """View for the team detail page.

    Args:
        request (HttpRequest): The request object.
        team_id (str): The UUID of the team.

    Returns:
        HttpResponse: The response object.

    """
    team = get_object_or_404(Team, id_uuid=team_id)

    # Get current date
    today = date.today()

    # Find the current season
    current_season = Season.objects.filter(
        start_date__lte=today, end_date__gte=today
    ).first()

    # If current season is not found, then find the next season
    if not current_season:
        current_season = Season.objects.filter(start_date__gte=today).first()

    # If next season is not found, then find the previous season
    if not current_season:
        current_season = Season.objects.filter(end_date__lte=today).last()

    # If no season is found, then there might be an error in data or there's currently
    # no active season
    if not current_season:
        raise Http404("No active season found")

    team_data = TeamData.objects.filter(team=team, season=current_season).first()

    user_request = request.user
    following = False
    coach = False
    if user_request.is_authenticated:
        player = Player.objects.get(user=user_request)
        following = player.team_follow.filter(id_uuid=team_id).exists()

        if team_data:
            coach = team_data.coach.filter(id_uuid=player.id_uuid).exists()
        else:
            coach = False

    context = {"team": team, "coaching": coach, "following": following}

    return render(request, "teams/detail.html", context)
