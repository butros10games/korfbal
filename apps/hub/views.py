from django.shortcuts import render, redirect
from django.utils import timezone
from django.db.models import Q, F, Value
from django.http import JsonResponse, Http404, HttpResponseRedirect
from django.db.models.functions import Concat

from apps.player.models import Player
from apps.team.models import Team, TeamData
from apps.schedule.models import Match, Season
from apps.game_tracker.models import Shot
from apps.club.models import Club
from apps.hub.models import PageConnectRegistration

from datetime import date


def index(request):
    ## get the players first upcoming match
    # get the player
    user_request = request.user
    if user_request.is_authenticated:
        player = Player.objects.get(user=user_request)
        # get the teams the player is connected to
        teams = Team.objects.filter(Q(team_data__players=player) | Q(team_data__coach=player)).distinct()
        # get the matches of the teams
        matches = Match.objects.filter(Q(home_team__in=teams) | Q(away_team__in=teams)).order_by('start_time')
        # get the first match that is in the future
        match = matches.filter(start_time__gte=timezone.now()).first()
    else:
        match = None
        
    context = {
        "display_back": True,
        "match": match,
        "match_date": match.start_time.strftime('%a, %d %b') if match else "No upcoming matches",
        "start_time": match.start_time.strftime('%H:%M') if match else "",
        "home_score": Shot.objects.filter(match=match, team=match.home_team, scored=True).count() if match else 0,
        "away_score": Shot.objects.filter(match=match, team=match.away_team, scored=True).count() if match else 0,
    }
        
    return render(request, "index.html", context)

def search(request):
    teams_json = []
    
    search_term = request.GET.get('q', '')
    category = request.GET.get('category', '')
    
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
    
    if category == 'teams':
        # Get the teams that match the search term
        teams = Team.objects.annotate(
        full_name=Concat(F('club__name'), Value(' '), F('name'))).filter(Q(full_name__icontains=search_term))
        
        for team in teams:
            team_data = TeamData.objects.filter(team=team, season=current_season).first()
            
            teams_json.append({
                "id": str(team.id_uuid),
                "name": team.__str__(),
                "img_url": team.club.logo.url if team.club.logo else None,
                "competition": team_data.competition if team_data else "",
                "url": str(team.get_absolute_url())
            })
        
    elif category == "clubs":
        # Get the teams that match the search term
        clubs = Club.objects.filter(Q(name__icontains=search_term))
        
        for club in clubs:
            teams_json.append({
                "id": str(club.id_uuid),
                "name": club.name,
                "img_url": club.logo.url if club.logo else None,
                "competition": None,
                "url": str(club.get_absolute_url())
            })
        
    context = {
        "teams": teams_json
    }
    
    return JsonResponse(context)

def previous_page(request):
    player = Player.objects.get(user=request.user)
    counter = request.session.get('back_counter', 1)
    pages = PageConnectRegistration.objects.filter(player=player).order_by('-registration_date').exclude(page='')

    if pages.count() > counter:
        referer = pages[counter].page
    else:
        referer = None

    request.session['back_counter'] = counter + 1
    request.session['is_back_navigation'] = True  # Set the flag

    if referer:
        return HttpResponseRedirect(referer)
    else:
        request.session['back_counter'] = 1
        return redirect('teams')
