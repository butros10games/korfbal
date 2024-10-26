from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse

from apps.player.models import Player
from apps.team.models import TeamData, Team
from apps.schedule.models import Match
from apps.game_tracker.models import PlayerGroup, MatchData, GroupTypes


def player_overview(request, match_id, team_id):
    match_model = get_object_or_404(Match, id_uuid=match_id)
    team_model = get_object_or_404(Team, id_uuid=team_id)
    
    match_data = MatchData.objects.get(match_link=match_model)
    
    player_groups = PlayerGroup.objects.filter(match_data=match_data, team=team_model)
    
    context = {
        "team_model": team_model,
        "player_groups": player_groups,
    }
    
    return render(request, "matches/players_selector.html", context)

def players_team(_, match_id, team_id):
    match_data = get_object_or_404(Match, id_uuid=match_id)
    team_model = get_object_or_404(Team, id_uuid=team_id)
    
    team_data = TeamData.objects.get(team=team_model, season=match_data.season)
    
    return JsonResponse({"players": [{"id": str(player.id_uuid), "name": player.user.username} for player in team_data.players.all()]})
    
def player_search(request):
    # get the name of the player that is searched for
    player_name = request.GET.get('player_name')
    
    players = Player.objects.filter(user__username__icontains=player_name)
    
    # return the players that are found in json format
    return JsonResponse({"players": [{"id": str(player.id_uuid), "name": player.user.username} for player in players]})

def player_selection(request, match_id, team_id):
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

def player_designation(request, match_id, team_id):
    match_data = get_object_or_404(Match, id_uuid=match_id)
    team_model = get_object_or_404(Team, id_uuid=team_id)
    
    # get the player ids from the request
    new_group_id = request.POST.getlist('new_group_id')
    old_group_id = request.POST.getlist('old_group_id')
    player_ids = request.POST.getlist('players')
    
    player_group_model = PlayerGroup.objects.get(match_data=match_data, team=team_model, starting_type=GroupTypes.objects.get(id_uuid=new_group_id))
    old_player_group_model = PlayerGroup.objects.get(match_data=match_data, team=team_model, starting_type=GroupTypes.objects.get(id_uuid=old_group_id))
    
    for player_id in player_ids:
        old_player_group_model.players.remove(Player.objects.get(id_uuid=player_id))
        player_group_model.players.add(Player.objects.get(id_uuid=player_id))
        
    return JsonResponse({"success": True})

def player_remove_group(request, match_id, team_id):
    match_data = get_object_or_404(Match, id_uuid=match_id)
    team_model = get_object_or_404(Team, id_uuid=team_id)
    
    old_group_id = request.POST.getlist('old_group_id')
    player_ids = request.POST.getlist('players')
    
    player_group_model = PlayerGroup.objects.get(match_data=match_data, team=team_model, starting_type=GroupTypes.objects.get(id_uuid=old_group_id))
    
    for player_id in player_ids:
        player_group_model.players.remove(Player.objects.get(id_uuid=player_id))
    
    return JsonResponse({"success": True})