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
    
    return JsonResponse({"players": [{"id": str(player.id_uuid), "name": player.user.username} for player in team_data.players.all()]})
    
def player_search(request):
    if request.method != "GET":
        return invalid_request
    
    if not request.GET.get('player_name'):
        return no_player_selected
    
    if len(request.GET.get('player_name')) < 3:
        return JsonResponse({"success": False, "error": "Player name should be at least 3 characters long"})
    
    if len(request.GET.get('player_name')) > 50:
        return JsonResponse({"success": False, "error": "Player name should be at most 50 characters long"})
    
    # get the name of the player that is searched for
    player_name = request.GET.get('player_name')
    
    players = Player.objects.filter(user__username__icontains=player_name)
    
    # return the players that are found in json format
    return JsonResponse({"players": [{"id": str(player.id_uuid), "name": player.user.username} for player in players]})

def player_selection(request, match_id, team_id):
    if request.method != "POST":
        return invalid_request
    
    if not request.POST.getlist('players'):
        return no_player_selected
    
    if len(request.POST.getlist('players')) > 16:
        return to_many_players_selected
    
    match_data = get_object_or_404(Match, id_uuid=match_id)
    team_model = get_object_or_404(Team, id_uuid=team_id)
    
    reserve_model = GroupTypes.objects.get(name="Reserve")
    player_group_model = PlayerGroup.objects.get(match_data=match_data, team=team_model, starting_type=reserve_model)
    
    # get the player ids from the request
    player_ids = request.POST.getlist('players')
    
    for player_id in player_ids:
        player_group_model.players.add(Player.objects.get(id_uuid=player_id))
    
    player_group_model.save()
        
    return JsonResponse({"success": True})

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
    
    if len(selected_players) > 4:
        return to_many_players_selected

    if new_group_id:
        player_group_model = PlayerGroup.objects.get(id_uuid=new_group_id)

    for player_data in selected_players:
        player_id = player_data.get('playerId')
        old_group_id = player_data.get('groupId')
        
        PlayerGroup.objects.get(id_uuid=old_group_id).players.remove(Player.objects.get(id_uuid=player_id))
        
        if player_group_model:
            player_group_model.players.add(Player.objects.get(id_uuid=player_id))

    return JsonResponse({"success": True})

def _get_player_groups(match_id, team_id):
    match_model = get_object_or_404(Match, id_uuid=match_id)
    team_model = get_object_or_404(Team, id_uuid=team_id)
    
    match_data = MatchData.objects.get(match_link=match_model)
    
    return PlayerGroup.objects.filter(match_data=match_data, team=team_model).order_by('starting_type__order')