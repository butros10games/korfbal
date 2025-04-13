"""This module contains the view for the match tracker page."""

from django.shortcuts import get_object_or_404, render

from apps.common.utils import get_time_display
from apps.game_tracker.models import MatchData, MatchPart, Pause, PlayerGroup, Shot
from apps.schedule.models import Match
from apps.team.models import Team


def match_tracker(request, match_id, team_id):
    """Render the match tracker page.

    Args:
        request: The request object.
        match_id: The id of the match.
        team_id: The id of the team.

    Returns:
        The rendered match tracker page.

    """
    match_model = get_object_or_404(Match, id_uuid=match_id)
    match_data = MatchData.objects.get(match_link=match_model)
    team_data = get_object_or_404(Team, id_uuid=team_id)

    # get the two teams that are playing and make the first team the team from team_data
    # and the second team the opponent
    if match_model.home_team == team_data:
        opponent_data = match_model.away_team
    else:
        opponent_data = match_model.home_team

    # calculate the score for both the teams
    team_1_score = Shot.objects.filter(
        match_data=match_data, team=team_data, scored=True
    ).count()
    team_2_score = Shot.objects.filter(
        match_data=match_data, team=opponent_data, scored=True
    ).count()

    # Check if the "aanval" and "verdediging" playerGroups are created for the both
    # teams
    team_names = [match_model.home_team, match_model.away_team]
    player_group_names = ["Aanval", "Verdediging"]

    for team_name in team_names:
        for group_name in player_group_names:
            PlayerGroup.objects.get(
                team=team_name, match_data=match_data, starting_type__name=group_name
            )

    button_text = "Start"
    if match_data.status == "active":
        if (
            Pause.objects.filter(match_data=match_data, active=True).exists()
            or not MatchPart.objects.filter(match_data=match_data, active=True).exists()
        ):
            button_text = "Start"
        else:
            button_text = "Pause"

    context = {
        "match": match_data,
        "time_display": get_time_display(match_data),
        "start_stop_button": button_text,
        "team_1": team_data,
        "team_1_score": team_1_score,
        "team_2": opponent_data,
        "team_2_score": team_2_score,
        "start_date": match_model.start_time.strftime("%A, %d %B"),
        "start_time": match_model.start_time.strftime("%H:%M"),
    }

    return render(request, "matches/tracker.html", context)
