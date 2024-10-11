from django.shortcuts import render, get_object_or_404, redirect
from django.http import Http404

from apps.team.models import Team, TeamData
from apps.player.models import Player
from apps.schedule.models import Season

from datetime import date


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
