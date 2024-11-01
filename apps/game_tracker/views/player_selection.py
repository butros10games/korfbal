from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse

from apps.player.models import Player
from apps.team.models import TeamData, Team
from apps.schedule.models import Match
from apps.game_tracker.models import PlayerGroup, MatchData, GroupTypes

import json

invalid_request = JsonResponse({"error": "Invalid request method"}, status=405)
json_error = JsonResponse({"error": "Invalid JSON data"}, status=400)
no_player_selected = JsonResponse({"error": "No player selected"}, status=400)
to_many_players_selected = JsonResponse({"error": "Too many players selected"}, status=400)

def player_overview(request, match_id, team_id):
    player_groups = _get_player_groups(match_id, team_id)
    
    context = {
        "team_name": player_groups[0].team.__str__(),
        "match_url": player_groups[0].match_data.match_link.get_absolute_url(),
        "player_groups": player_groups,
    }
    
    return render(request, "matches/players_selector.html", context)

def player_overview_data(_, match_id, team_id):
    player_groups = _get_player_groups(match_id, team_id)
    
    player_groups_data = []
    for player_group in player_groups:
        players_data = []
        for player in player_group.players.all():
            players_data.append({
                "id_uuid": str(player.id_uuid),
                "user": {
                    "username": player.user.username
                },
                "get_profile_picture": player.get_profile_picture()
            })
        player_groups_data.append({
            "id_uuid": str(player_group.id_uuid),
            "starting_type": {
                "name": player_group.starting_type.name
            },
            "players": players_data
        })
    
    return JsonResponse({"player_groups": player_groups_data})

def players_team(_, match_id, team_id):
    match_data = get_object_or_404(Match, id_uuid=match_id)
    team_model = get_object_or_404(Team, id_uuid=team_id)
    
    team_data = TeamData.objects.get(team=team_model, season=match_data.season)
    
    ## remove the players that are already in a player group
    players = team_data.players.all()
    player_groups = PlayerGroup.objects.filter(match_data=MatchData.objects.get(match_link=match_data), team=team_model)
    
    for player_group in player_groups:
        players = players.exclude(id_uuid__in=player_group.players.all())
        
    return JsonResponse({"players": [{"id_uuid": str(player.id_uuid), "user": { "username": player.user.username }, "get_profile_picture": player.get_profile_picture()} for player in players]})
    
def player_search(request, match_id, team_id):
    if request.method != "GET":
        return invalid_request
    
    if not request.GET.get('search'):
        return no_player_selected
    
    if len(request.GET.get('search')) < 3:
        return JsonResponse({"success": False, "error": "Player name should be at least 3 characters long"})
    
    if len(request.GET.get('search')) > 50:
        return JsonResponse({"success": False, "error": "Player name should be at most 50 characters long"})
    
    match_data = get_object_or_404(Match, id_uuid=match_id)
    team_model = get_object_or_404(Team, id_uuid=team_id)
    
    # get the name of the player that is searched for
    player_name = request.GET.get('search')
    
    player_groups = PlayerGroup.objects.filter(match_data=MatchData.objects.get(match_link=match_data), team=team_model)
    
    players = Player.objects.filter(user__username__icontains=player_name).exclude(id_uuid__in=[player.id_uuid for player_group in player_groups for player in player_group.players.all()])
    
    # return the players that are found in json format
    return JsonResponse({"players": [{"id_uuid": str(player.id_uuid), "user": { "username": player.user.username }, "get_profile_picture": player.get_profile_picture() } for player in players]})

def player_designation(request):
    if request.method != "POST":
        return invalid_request
    
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return json_error
    
    selected_players = data.get('players', [])
    new_group_id = data.get('new_group_id')
    
    if not selected_players:
        return no_player_selected

    if new_group_id:
        player_group_model = PlayerGroup.objects.get(id_uuid=new_group_id)
        
        # check if the player group is reserve or a other group
        if player_group_model.starting_type.name == "Reserve":
            if len(selected_players) > 16:
                return to_many_players_selected
        elif len(selected_players) > 4:
            return to_many_players_selected

    for player_data in selected_players:
        player_id = player_data.get('id_uuid')
        old_group_id = player_data.get('groupId')
        
        if old_group_id:
            PlayerGroup.objects.get(id_uuid=old_group_id).players.remove(Player.objects.get(id_uuid=player_id))
        
        if new_group_id:
            player_group_model.players.add(Player.objects.get(id_uuid=player_id))

    return JsonResponse({"success": True})

def _get_player_groups(match_id, team_id):
    match_model = get_object_or_404(Match, id_uuid=match_id)
    team_model = get_object_or_404(Team, id_uuid=team_id)
    
    match_data = MatchData.objects.get(match_link=match_model)
    
    return PlayerGroup.objects.filter(match_data=match_data, team=team_model).order_by('starting_type__order')