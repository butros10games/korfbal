from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.db.models import Q
from django.http import Http404

from apps.team.models import Team, TeamData
from apps.player.models import Player
from apps.club.models import Club
from apps.schedule.models import Season

from datetime import date
import json

def teams(request):
    connected_teams = None
    following_teams = None
    user = request.user
    if user.is_authenticated:
        # Get the Player object associated with this user
        player = Player.objects.get(user=user)
        
        connected_teams = Team.objects.filter(Q(team_data__players=player) | Q(team_data__coach=player)).distinct()
        
        # Get all teams the user is following
        following_teams = player.team_follow.all()
        
        # remove the teams the user is part of from the teams the user is following
        following_teams = following_teams.exclude(id_uuid__in=connected_teams)
    
    context = {
        "connected": connected_teams,
        "following": following_teams,
        "display_back": True
    }
    return render(request, "teams/index.html", context)

def teams_index_data(request):
    connected_list = []
    following_list = []
    selection = None
    
    user = request.user
                
    if request.method == 'POST':
        # Load the JSON data from the request body
        data = json.loads(request.body.decode('utf-8'))

        # Check if the 'value' key is in the data
        if 'value' in data:
            selection = data['value']

            if selection == "clubs" and user.is_authenticated:
                player = Player.objects.get(user=user)
                
                connected_clubs = Club.objects.filter(Q(teams__team_data__players=player) | Q(teams__team_data__coach=player)).distinct()
                
                following_clubs = player.club_follow.all()
                
                following_clubs = following_clubs.exclude(id_uuid__in=connected_clubs)
                
                for club in connected_clubs:
                    connected_list.append({
                        "id": str(club.id_uuid),
                        "name": club.name,
                        "img_url": club.logo.url if club.logo else None,
                        "competition": None,
                        "url": str(club.get_absolute_url())
                    })
                    
                for club in following_clubs:
                    following_list.append({
                        "id": str(club.id_uuid),
                        "name": club.name,
                        "img_url": club.logo.url if club.logo else None,
                        "competition": None,
                        "url": str(club.get_absolute_url())
                    })
            
            elif selection == "teams" and user.is_authenticated:
                # Get the Player object associated with this user
                player = Player.objects.get(user=user)
                
                # Get all teams where the user is part of the team
                connected_teams = Team.objects.filter(Q(team_data__players=player) | Q(team_data__coach=player)).distinct()
                
                # remove duplicate teams
                connected_teams = connected_teams.distinct()
                
                # Get all teams the user is following
                following_teams = player.team_follow.all()
                
                # remove the teams the user is part of from the teams the user is following
                following_teams = following_teams.exclude(id_uuid__in=connected_teams)
                
                for team in connected_teams:
                    connected_list.append({
                        "id": str(team.id_uuid),
                        "name": team.__str__(),
                        "img_url": team.club.logo.url if team.club.logo else None,
                        "competition": team.team_data.last().competition if team.team_data else "",
                        "url": str(team.get_absolute_url())
                    })
                    
                for team in following_teams:
                    following_list.append({
                        "id": str(team.id_uuid),
                        "name": team.__str__(),
                        "img_url": team.club.logo.url if team.club.logo else None,
                        "competition": team.team_data.last().competition if team.team_data else "",
                        "url": str(team.get_absolute_url())
                    })
    
    context = {
        "type": selection,
        "connected": connected_list,
        "following": following_list
    }
    
    return JsonResponse(context)

def team_detail(request, team_id):
    team = get_object_or_404(Team, id_uuid=team_id)
    
    # Get current date
    today = date.today()
    
    # Find the current season
    current_season = Season.objects.filter(start_date__lte=today, end_date__gte=today).first()
    
    # If current season is not found, then find the next season
    if not current_season:
        current_season = Season.objects.filter(start_date__gte=today).first()
    
    # If next season is not found, then find the previous season
    if not current_season:
        current_season = Season.objects.filter(end_date__lte=today).last()

    # If no season is found, then there might be an error in data or there's currently no active season
    if not current_season:
        raise Http404("No active season found")
    
    team_data = TeamData.objects.filter(team=team, season=current_season).first()
    
    user_request = request.user
    following = False
    coach = False
    if user_request.is_authenticated:
        player = Player.objects.get(user=user_request)
        following = player.team_follow.filter(id_uuid=team_id).exists()
        
        if team_data:
            coach = team_data.coach.filter(id_uuid=player.id_uuid).exists()
        else: 
            coach = False
        
    context= {
        "team": team,
        "coaching": coach,
        "following": following
    }
    
    return render(request, "teams/detail.html", context)

# this view handels the registration of a player to a team. 
# if the user is logedin the users gets added to the team if the user is not registerd the user gets redirected to the login page with a next parameter
def register_to_team(request, team_id):
    team = get_object_or_404(Team, id_uuid=team_id)
    user = request.user
    
    try:
        season = Season.objects.get(start_date__lte=date.today(), end_date__gte=date.today())
    except Season.DoesNotExist:
        season = Season.objects.filter(end_date__lte=date.today()).order_by('-end_date').first()
    
    if user.is_authenticated:
        player = Player.objects.get(user=user)
        
        try:
            team_data = TeamData.objects.get(team=team, season=season)
        except TeamData.DoesNotExist:
            # get the coach of the previous season
            try:
                previous_season = Season.objects.filter(end_date__lte=date.today()).order_by('-end_date').first()
                previous_team_data = TeamData.objects.get(team=team, season=previous_season)
                
                team_data = TeamData.objects.create(team=team, season=season)
                team_data.coach.set(previous_team_data.coach.all())
            except TeamData.DoesNotExist:
                team_data = TeamData.objects.create(team=team, season=season)
        
        team_data.players.add(player)
        
        return redirect('teams')
    else:
        return redirect('/login/?next=/register_to_team/%s/' % team_id)
