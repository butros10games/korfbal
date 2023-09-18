from django.shortcuts import render, redirect, get_object_or_404
from .models import Team, Player, TeamData

# Create your views here.
def index(request):
    return render(request, "index.html")

def teams(request):
    user = request.user
    if not user.is_authenticated:
        return redirect('login')
    
    # Get the Player object associated with this user
    player = Player.objects.get(user=user)
    
    # Get all teams where the user is part of the team
    connected_teams = Team.objects.filter(team_data__players=player)
    
    # Get all teams the user is following
    following_teams = player.team_follow.all()
    
    context = {
        "teams": connected_teams,
        "following_teams": following_teams,
    }
    return render(request, "teams/index.html", context)

def team_detail(request, team_id):
    team = get_object_or_404(Team, id_uuid=team_id)
    
    return render(request, "teams/detail.html", {"team": team})

def profile(request):
    user = request.user
    
    if not user.is_authenticated:
        return redirect('login')
    
    player = Player.objects.get(user=user)
    
    context = {
        "player": player,
    }
    
    return render(request, "profile/index.html", context)