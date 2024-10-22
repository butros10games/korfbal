from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.csrf import csrf_exempt

from apps.game_tracker.models import MatchData, PlayerGroup, Shot, Pause, MatchPart
from apps.player.models import Player
from apps.team.models import Team, TeamData
from apps.schedule.models import Match


def match_detail(request, match_id):
    match_model = get_object_or_404(Match, id_uuid=match_id)
    
    match_data = MatchData.objects.get(match_link=match_model)
    
    context = {
        "match": match_model,
        "match_data": match_data,
        "time_display": get_time_display(match_data),
        "start_date": match_model.start_time.strftime('%A, %d %B'),
        "start_time": match_model.start_time.strftime('%H:%M'),
        "home_score": Shot.objects.filter(match_data=match_data, team=match_model.home_team, scored=True).count(),
        "away_score": Shot.objects.filter(match_data=match_data, team=match_model.away_team, scored=True).count()
    }
    
    return render(request, "matches/detail.html", context)

def match_team_selector(request, match_id):
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
        return redirect('match_tracker', match_id=match_id, team_id=connected_teams[0].id_uuid)

    context = {
        "match": match_data
    }
    
    return render(request, "matches/team_selector.html", context)

def match_tracker(request, match_id, team_id):
    match_model = get_object_or_404(Match, id_uuid=match_id)
    match_data = MatchData.objects.get(match_link=match_model)
    team_data = get_object_or_404(Team, id_uuid=team_id)
    
    # get the two teams that are playing and make the first team the team from team_data and the second team the opponent
    if match_model.home_team == team_data:
        opponent_data = match_model.away_team
    else:
        opponent_data = match_model.home_team

    # calculate the score for both the teams
    team_1_score = Shot.objects.filter(match_data=match_data, team=team_data, scored=True).count()
    team_2_score = Shot.objects.filter(match_data=match_data, team=opponent_data, scored=True).count()
    
    ## Check if the 'aanval' and 'verdediging' playerGroups are created for the both teams
    team_names = [match_model.home_team, match_model.away_team]
    player_group_names = ['Aanval', 'Verdediging']

    for team_name in team_names:
        for group_name in player_group_names:
            PlayerGroup.objects.get_or_create(team=team_name, match_data=match_data, starting_type__name=group_name)
    
    button_text = "Start"
    if match_data.status == 'active':
        if Pause.objects.filter(match_data=match_data, active=True).exists() or not MatchPart.objects.filter(match_data=match_data, active=True).exists():
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
        "start_date": match_model.start_time.strftime('%A, %d %B'),
        "start_time": match_model.start_time.strftime('%H:%M')
    }
    
    return render(request, "matches/tracker.html", context)

def get_time_display(match_data):
    time_left = match_data.part_lenght
        
    # convert the seconds to minutes and seconds to display on the page make the numbers look nice with the %02d
    minutes = int(time_left / 60)
    seconds = int(time_left % 60)
    return "%02d:%02d" % (minutes, seconds)