from django.shortcuts import render, redirect, get_object_or_404
from .models import Team, Player, TeamData, Season
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.http import Http404

from datetime import date

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
        
        ## remove the teams the user is following from the teams the user is part of
        following_teams = following_teams.exclude(id_uuid__in=connected_teams)
    
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
    
    # Get current date
    today = date.today()
    
    # Find the current season
    current_season = Season.objects.filter(start_date__lte=today, end_date__gte=today).first()

    # If no season is found, then there might be an error in data or there's currently no active season
    if not current_season:
        raise Http404("No active season found")
    
    team_data = TeamData.objects.filter(team=team, season=current_season).first()
    
    profile_url = None
    user_request = request.user
    if user_request.is_authenticated:
        player = Player.objects.get(user=user_request)
        profile_url = player.get_absolute_url
        
    following = False
    coach = False
    if user_request.is_authenticated:
        player = Player.objects.get(user=user_request)
        following = player.team_follow.filter(id_uuid=team_id).exists()
        
        coach = team_data.coach.filter(id_uuid=player.id_uuid).exists()
        
    context= {
        "team": team,
        "profile_url": profile_url,
        "coaching": coach,
        "following": following
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
        "profile_url": profile_url,
    }
    
    return render(request, "profile/index.html", context)

@csrf_exempt
def upload_profile_picture(request):
    if request.method == 'POST' and request.FILES['profile_picture']:
        profile_picture = request.FILES['profile_picture']
        
        # Assuming you have a Player model with a profile_picture field
        player = Player.objects.get(user=request.user)
        player.profile_picture.save(profile_picture.name, profile_picture)

        # Return the URL of the uploaded image
        return JsonResponse({'url': player.profile_picture.url})

    return JsonResponse({'error': 'Invalid request'}, status=400)