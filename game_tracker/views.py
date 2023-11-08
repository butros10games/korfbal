from django.shortcuts import render, redirect, get_object_or_404
from .models import Team, Player, TeamData, Season, Club, Match, Goal
from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.http import Http404

from datetime import date
import json

# Create your views here.
def index(request):
    profile_url, profile_img_url = standart_inports(request)
        
    context = {
        "profile_url": profile_url,
        "profile_img_url": profile_img_url
    }
        
    return render(request, "index.html", context)

def club_detail(request, club_id):
    club = get_object_or_404(Club, id_uuid=club_id)
    
    profile_url, profile_img_url = standart_inports(request)
    
    user_request = request.user
    admin = False
    following = False
    if user_request.is_authenticated:
        player = Player.objects.get(user=user_request)
        admin = club.admin.filter(id_uuid=player.id_uuid).exists()
        
        following = player.club_follow.filter(id_uuid=club_id).exists()
    
    context = {
        "club": club,
        "admin": admin,
        "following": following,
        "profile_url": profile_url,
        "profile_img_url": profile_img_url
    }
    
    return render(request, "club/detail.html", context)

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
        
        # remove the teams the user is part of from the teams the user is following
        following_teams = following_teams.exclude(id_uuid__in=connected_teams)
    
    profile_url, profile_img_url = standart_inports(request)
    
    context = {
        "connected": connected_teams,
        "following": following_teams,
        "profile_url": profile_url,
        "profile_img_url": profile_img_url
    }
    return render(request, "teams/index.html", context)

@csrf_exempt
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
                
                connected_clubs = Club.objects.filter(teams__team_data__players=player).distinct()
                
                following_clubs = player.club_follow.all()
                
                following_clubs = following_clubs.exclude(id_uuid__in=connected_clubs)
                
                for club in connected_clubs:
                    connected_list.append({
                        "id": str(club.id_uuid),
                        "name": club.name,
                        "url": str(club.get_absolute_url())
                    })
                    
                for club in following_clubs:
                    following_list.append({
                        "id": str(club.id_uuid),
                        "name": club.name,
                        "url": str(club.get_absolute_url())
                    })
            
            elif selection == "teams" and user.is_authenticated:
                # Get the Player object associated with this user
                player = Player.objects.get(user=user)
                
                # Get all teams where the user is part of the team
                connected_teams = Team.objects.filter(team_data__players=player)
                
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
                        "url": str(team.get_absolute_url())
                    })
                    
                for team in following_teams:
                    following_list.append({
                        "id": str(team.id_uuid),
                        "name": team.__str__(),
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
    
    profile_url, profile_img_url = standart_inports(request)
    
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
        "profile_url": profile_url,
        "profile_img_url": profile_img_url,
        "coaching": coach,
        "following": following
    }
    
    return render(request, "teams/detail.html", context)

def search(request):
    teams_json = []
    
    search_term = request.GET.get('q', '')
    category = request.GET.get('category', '')
    
    if category == 'teams':
        # Get the teams that match the search term
        teams = Team.objects.filter(Q(name__icontains=search_term))
        
        for team in teams:
            teams_json.append({
                "id": str(team.id_uuid),
                "name": team.__str__(),
                "url": str(team.get_absolute_url())
            })
        
    elif category == "clubs":
        # Get the teams that match the search term
        clubs = Club.objects.filter(Q(name__icontains=search_term))
        
        for club in clubs:
            teams_json.append({
                "id": str(club.id_uuid),
                "name": club.name,
                "url": str(club.get_absolute_url())
            })
        
    context = {
        "teams": teams_json
    }
    
    return JsonResponse(context)

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
    
    profile_url, profile_img_url = standart_inports(request)
    
    context = {
        "player": player,
        "user_data": user_data,
        "is_own_profile": is_own_profile,
        "profile_url": profile_url,
        "profile_img_url": profile_img_url
    }
    
    return render(request, "profile/index.html", context)

def match_detail(request, match_id):
    match_data = get_object_or_404(Match, id_uuid=match_id)
    
    profile_url, profile_img_url = standart_inports(request)
    
    context = {
        "match": match_data,
        "start_date": match_data.start_time.strftime('%A, %d %B'),
        "start_time": match_data.start_time.strftime('%H:%M'),
        "profile_url": profile_url,
        "profile_img_url": profile_img_url,
        "home_score": Goal.objects.filter(match=match_data, team=match_data.home_team).count(),
        "away_score": Goal.objects.filter(match=match_data, team=match_data.away_team).count()
    }
    
    return render(request, "matches/detail.html", context)

def match_team_selector(request, match_id):
    match_data = get_object_or_404(Match, id_uuid=match_id)
    
    profile_url, profile_img_url = standart_inports(request)
    
    context = {
        "match": match_data,
        "profile_url": profile_url,
        "profile_img_url": profile_img_url
    }
    
    return render(request, "matches/team_selector.html", context)

def match_tracker(request, match_id, team_id):
    match_data = get_object_or_404(Match, id_uuid=match_id)
    team_data = get_object_or_404(Team, id_uuid=team_id)
    
    profile_url, profile_img_url = standart_inports(request)
    
    # get the two teams that are playing and make the first team the team from team_data and the second team the opponent
    if match_data.home_team == team_data:
        opponent_data = match_data.away_team
    else:
        opponent_data = match_data.home_team
        
    # calculate the time left in the current part if the part is not finished or started yet then set the time to the part lenght i have set the part_lenght to be in seconds
    time_left = match_data.part_lenght
    if match_data.current_part > 1:
        time_left = match_data.part_lenght - ((match_data.current_part - 1) * match_data.part_lenght)
        
    # convert the seconds to minutes and seconds to display on the page make the numbers look nice with the %02d
    minutes = int(time_left / 60)
    seconds = int(time_left % 60)
    time_display = "%02d:%02d" % (minutes, seconds)
    
    # calculate the score for both the teams
    team_1_score = Goal.objects.filter(match=match_data, team=team_data).count()
    team_2_score = Goal.objects.filter(match=match_data, team=opponent_data).count()
    
    context = {
        "match": match_data,
        "minutes": minutes,
        "seconds": seconds,
        "time_display": time_display,
        "team_1": team_data,
        "team_1_score": team_1_score,
        "team_2": opponent_data,
        "team_2_score": team_2_score,
        "start_date": match_data.start_time.strftime('%A, %d %B'),
        "start_time": match_data.start_time.strftime('%H:%M'),
        "profile_url": profile_url,
        "profile_img_url": profile_img_url
    }
    
    return render(request, "matches/tracker.html", context)

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

def standart_inports(request):
    profile_url = None
    profile_img_url = None
    user_request = request.user
    if user_request.is_authenticated:
        player = Player.objects.get(user=user_request)
        profile_url = player.get_absolute_url
        profile_img_url = player.profile_picture.url if player.profile_picture else None
        
    return profile_url, profile_img_url

# this view handels the registration of a player to a team. if the user is logedin the users gets added to the team if the user is not registerd the user gets redirected to the login page with a next parameter
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