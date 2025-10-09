"""Module contains the view for the team registration page."""

from django.http import HttpRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone

from apps.player.models import Player
from apps.schedule.models import Season
from apps.team.models import Team, TeamData


def register_to_team(request: HttpRequest, team_id: str) -> HttpResponseRedirect:
    """View for the team registration page.

    Args:
        request (HttpRequest): The request object.
        team_id (str): The UUID of the team.

    Returns:
        HttpResponse: The response object.

    """
    team: Team = get_object_or_404(Team, id_uuid=team_id)
    user = request.user

    try:
        season = Season.objects.get(
            start_date__lte=timezone.now().date(),
            end_date__gte=timezone.now().date(),
        )
    except Season.DoesNotExist:
        season = (
            Season.objects.filter(end_date__lte=timezone.now().date())
            .order_by("-end_date")
            .first()
        )

    if user.is_authenticated:
        try:
            player: Player = Player.objects.get(user=user)
        except Player.DoesNotExist:
            return redirect("teams")

        try:
            team_data: TeamData = TeamData.objects.get(team=team, season=season)
        except TeamData.DoesNotExist:
            # get the coach of the previous season
            try:
                previous_season: Season | None = (
                    Season.objects.filter(end_date__lte=timezone.now().date())
                    .order_by("-end_date")
                    .first()
                )
                previous_team_data: TeamData | None = TeamData.objects.get(
                    team=team,
                    season=previous_season,
                )

                assert previous_team_data is not None
                team_data = TeamData.objects.create(team=team, season=season)
                team_data.coach.set(previous_team_data.coach.all())
            except TeamData.DoesNotExist:
                team_data = TeamData.objects.create(team=team, season=season)

        team_data.players.add(player)

        return redirect("teams")
    return redirect(f"/login/?next=/register_to_team/{team_id}/")
