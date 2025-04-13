"""Module contains the view for the team registration page."""

from datetime import date

from django.shortcuts import get_object_or_404, redirect

from apps.player.models import Player
from apps.schedule.models import Season
from apps.team.models import Team, TeamData


def register_to_team(request, team_id):
    """View for the team registration page.

    Args:
        request (HttpRequest): The request object.
        team_id (str): The UUID of the team.

    Returns:
        HttpResponse: The response object.

    """
    team = get_object_or_404(Team, id_uuid=team_id)
    user = request.user

    try:
        season = Season.objects.get(
            start_date__lte=date.today(), end_date__gte=date.today()
        )
    except Season.DoesNotExist:
        season = (
            Season.objects.filter(end_date__lte=date.today())
            .order_by("-end_date")
            .first()
        )

    if user.is_authenticated:
        player = Player.objects.get(user=user)

        try:
            team_data = TeamData.objects.get(team=team, season=season)
        except TeamData.DoesNotExist:
            # get the coach of the previous season
            try:
                previous_season = (
                    Season.objects.filter(end_date__lte=date.today())
                    .order_by("-end_date")
                    .first()
                )
                previous_team_data = TeamData.objects.get(
                    team=team, season=previous_season
                )

                team_data = TeamData.objects.create(team=team, season=season)
                team_data.coach.set(previous_team_data.coach.all())
            except TeamData.DoesNotExist:
                team_data = TeamData.objects.create(team=team, season=season)

        team_data.players.add(player)

        return redirect("teams")
    else:
        return redirect(f"/login/?next=/register_to_team/{team_id}/")
