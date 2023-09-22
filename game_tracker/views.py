from django.shortcuts import render, redirect, get_object_or_404
from .models import Team, Player, TeamData

# Create your views here.
def index(request):
    profile_url = None
    user_request = request.user
    if user_request.is_authenticated:
        player = Player.objects.get(user=user_request)
        profile_url = player.get_absolute_url
        
    context = {
        "profile_url": profile_url
    }
        
    return render(request, "index.html", context)

def teams(request):
    connected_teams = None
    following_teams = None
    user = request.user
    if user.is_authenticated:
        # Get the Player object associated with this user
        player = Player.objects.get(user=user)
        
        # Get all teams where the user is part of the team
        connected_teams = Team.objects.filter(team_data__players=player)
        
        # Get all teams the user is following
        following_teams = player.team_follow.all()
    
    profile_url = None
    user_request = request.user
    if user_request.is_authenticated:
        player = Player.objects.get(user=user_request)
        profile_url = player.get_absolute_url
    
    context = {
        "teams": connected_teams,
        "following_teams": following_teams,
        "profile_url": profile_url
    }
    return render(request, "teams/index.html", context)

def team_detail(request, team_id):
    team = get_object_or_404(Team, id_uuid=team_id)
    
    profile_url = None
    user_request = request.user
    if user_request.is_authenticated:
        player = Player.objects.get(user=user_request)
        profile_url = player.get_absolute_url
    
    context= {
        "team": team,
        "profile_url": profile_url
    }
    
    return render(request, "teams/detail.html", context)

def profile_detail(request, player_id=None):
    player = None
    user = request.user
    
    if player_id:
        player = get_object_or_404(Player, id_uuid=player_id)
        user_data = player.user
    elif user.is_authenticated:
        player = Player.objects.get(user=user)
        
    # Check if the user is viewing their own profile
    is_own_profile = False
    if user.is_authenticated and user == player.user:
        is_own_profile = True
    
    profile_url = None
    user_request = request.user
    if user_request.is_authenticated:
        player = Player.objects.get(user=user_request)
        profile_url = player.get_absolute_url
    
    context = {
        "player": player,
        "user_data": user_data,
        "is_own_profile": is_own_profile,
        "profile_url": profile_url
    }
    
    return render(request, "profile/index.html", context)

def match_detail(request):
    profile_url = None
    user_request = request.user
    if user_request.is_authenticated:
        player = Player.objects.get(user=user_request)
        profile_url = player.get_absolute_url
    
    context = {
        "profile_url": profile_url
    }
    
    return render(request, "profile/index.html", context)