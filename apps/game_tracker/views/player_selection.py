from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse

from apps.player.models import Player
from apps.team.models import TeamData, Team
from apps.schedule.models import Match
from apps.game_tracker.models import MatchPlayer, PlayerGroup, MatchData


def player_overview(request, match_id, team_id):
    match_model = get_object_or_404(Match, id_uuid=match_id)
    team_model = get_object_or_404(Team, id_uuid=team_id)
    
    match_data = MatchData.objects.get(match_link=match_model)
    
    player_groups = PlayerGroup.objects.filter(match_data=match_data, team=team_model).order_by('starting_type')
    
    # Extract UUIDs of players in player groups to avoid queryset issues
    player_uuids = [player.id_uuid for player_group in player_groups for player in player_group.players.all()]
    
    # Exclude players with these UUIDs from match players
    match_players = MatchPlayer.objects.filter(match_data=match_data, team=team_model).exclude(player__id_uuid__in=player_uuids)
    
    context = {
        "team_model": team_model,
        "player_groups": player_groups,
        "substitutes": match_players
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
    
    # get the player ids from the request
    player_ids = request.POST.getlist('players')
    
    # add all the players to the match players model
    for player_id in player_ids:
        MatchPlayer.objects.get_or_create(match_data=match_data, team=team_model, player=Player.objects.get(id_uuid=player_id))
        
    return JsonResponse({"success": True})

def player_designation(request, match_id, team_id):
    match_data = get_object_or_404(Match, id_uuid=match_id)
    team_model = get_object_or_404(Team, id_uuid=team_id)
    
    # get the player ids from the request
    player_group_data = request.POST.getlist('player_group')
    
    player_group_type = player_group_data.group_type
    player_group_model = PlayerGroup.objects.get(match_data=match_data, team=team_model, starting_type=player_group_type)
    
    for player_id in player_group_data.player_ids:
        player_group_model.players.add(Player.objects.get(id_uuid=player_id))
        
    return JsonResponse({"success": True})
