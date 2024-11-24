"""This module contains the view for the match team selector page."""

from apps.player.models import Player
from apps.schedule.models import Match
from apps.team.models import TeamData
from django.shortcuts import get_object_or_404, redirect, render


def match_team_selector(request, match_id):
    """
    Render the match team selector page.

    Args:
        request: The request object.
        match_id: The id of the match.

    Returns:
        The rendered match team selector page.
    """
    # Retrieve the match or return 404
    match_data = get_object_or_404(Match, id_uuid=match_id)

    # Get the teams in the match
    teams_in_match = [match_data.home_team, match_data.away_team]

    player = Player.objects.get(user=request.user)

    # Get the teams the user is connected to through TeamData as a player
    user_team_data_as_player = TeamData.objects.filter(players=player)
    user_teams_as_player = [team_data.team for team_data in user_team_data_as_player]

    # Get the teams the user is connected to through TeamData as a coach
    user_team_data_as_coach = TeamData.objects.filter(coach=player)
    user_teams_as_coach = [team_data.team for team_data in user_team_data_as_coach]

    # Combine the teams where the user is a player and a coach
    user_teams = list(set(user_teams_as_player + user_teams_as_coach))

    # Check if the user is connected to one or both of the teams in the match
    connected_teams = [team for team in teams_in_match if team in user_teams]

    # If the user is connected to only one team, redirect them to the tracker page
    if len(connected_teams) == 1:
        return redirect(
            "match_tracker", match_id=match_id, team_id=connected_teams[0].id_uuid
        )

    context = {"match": match_data}

    return render(request, "matches/team_selector.html", context)
