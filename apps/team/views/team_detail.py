"""Module contains the view for the team detail page."""

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from apps.player.models import Player
from apps.schedule.models import Season
from apps.team.models import Team, TeamData


def team_detail(request: HttpRequest, team_id: str) -> HttpResponse:
    """View for the team detail page.

    Args:
        request (HttpRequest): The request object.
        team_id (str): The UUID of the team.

    Returns:
        HttpResponse: The response object.

    """
    team: Team = get_object_or_404(Team, id_uuid=team_id)

    # Get current date (timezone-aware)
    today = timezone.now().date()

    # Find the current season
    current_season: Season | None = Season.objects.filter(
        start_date__lte=today,
        end_date__gte=today,
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

    team_data: TeamData | None = TeamData.objects.filter(
        team=team,
        season=current_season,
    ).first()

    user_request = request.user
    following: bool = False
    coach: bool = False
    if user_request.is_authenticated:
        player: Player = Player.objects.get(user=user_request)
        following = player.team_follow.filter(id_uuid=team_id).exists()

        if team_data:
            coach = team_data.coach.filter(id_uuid=player.id_uuid).exists()
        else:
            coach = False

    context = {"team": team, "coaching": coach, "following": following}

    return render(request, "teams/detail.html", context)
