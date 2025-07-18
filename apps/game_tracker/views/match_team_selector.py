"""Module contains the view for the match team selector page."""

from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render

from apps.player.models import Player
from apps.schedule.models import Match
from apps.team.models import Team, TeamData


def match_team_selector(
    request: HttpRequest, match_id: str,
) -> HttpResponse | HttpResponseRedirect:
    """Render the match team selector page.

    Args:
        request: The request object.
        match_id: The id of the match.

    Returns:
        The rendered match team selector page.

    """
    # Retrieve the match or return 404
    match_data: Match = get_object_or_404(Match, id_uuid=match_id)

    # Get the teams in the match
    teams_in_match: list[Team] = [match_data.home_team, match_data.away_team]

    player: Player = Player.objects.get(user=request.user)

    # Get the teams the user is connected to through TeamData as a player
    user_team_data_as_player: QuerySet[TeamData] = TeamData.objects.filter(
        players=player,
    )
    user_teams_as_player: list[Team] = [
        team_data.team for team_data in user_team_data_as_player
    ]

    # Get the teams the user is connected to through TeamData as a coach
    user_team_data_as_coach: QuerySet[TeamData] = TeamData.objects.filter(coach=player)
    user_teams_as_coach: list[Team] = [
        team_data.team for team_data in user_team_data_as_coach
    ]

    # Combine the teams where the user is a player and a coach
    user_teams: list[Team] = list(set(user_teams_as_player + user_teams_as_coach))

    # Check if the user is connected to one or both of the teams in the match
    connected_teams: list[Team] = [
        team for team in teams_in_match if team in user_teams
    ]

    # If the user is connected to only one team, redirect them to the tracker page
    if len(connected_teams) == 1:
        return redirect(
            "match_tracker", match_id=match_id, team_id=connected_teams[0].id_uuid,
        )

    context: dict = {"match": match_data}

    return render(request, "matches/team_selector.html", context)
