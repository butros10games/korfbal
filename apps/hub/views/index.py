from django.shortcuts import render
from django.utils import timezone
from django.db.models import Q

from apps.player.models import Player
from apps.team.models import Team
from apps.schedule.models import Match
from apps.game_tracker.models import Shot


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
        
    return render(request, "hub/index.html", context)