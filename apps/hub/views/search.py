from django.db.models import Q, F, Value
from django.http import JsonResponse, Http404
from django.db.models.functions import Concat

from apps.team.models import Team, TeamData
from apps.schedule.models import Season
from apps.club.models import Club

from datetime import date


def get_current_season():
    today = date.today()
    # Attempt to find the current season
    season = Season.objects.filter(start_date__lte=today, end_date__gte=today).first()
    # If not found, look for the next upcoming season
    if not season:
        season = Season.objects.filter(start_date__gte=today).first()
    # If still not found, look for the most recent past season
    if not season:
        season = Season.objects.filter(end_date__lte=today).last()
    # If no season is found, raise an error
    if not season:
        season = None
    return season


def serialize_team(team, current_season):
    team_data = TeamData.objects.filter(team=team, season=current_season).first()
    return {
        "id": str(team.id_uuid),
        "name": str(team),
        "img_url": team.club.get_club_logo(),
        "competition": team_data.competition if team_data else "",
        "url": team.get_absolute_url(),
    }


def serialize_club(club):
    return {
        "id": str(club.id_uuid),
        "name": club.name,
        "img_url": club.get_club_logo(),
        "competition": None,
        "url": club.get_absolute_url(),
    }


def search(request):
    search_term = request.GET.get("q", "")
    category = request.GET.get("category", "")

    results = []

    if category == "teams":
        current_season = get_current_season()

        #! Moet anders gedaan worden even uitzoeken hoe er alsnog een antwoord gestuurd kan worden
        if not current_season:
            return JsonResponse({"results": results})

        # Annotate teams with full name (club name + team name) and filter by search term
        teams = Team.objects.annotate(
            full_name=Concat(F("club__name"), Value(" "), F("name"))
        ).filter(full_name__icontains=search_term)

        # Serialize each team and add to results
        results = [serialize_team(team, current_season) for team in teams]

    elif category == "clubs":
        # Filter clubs by search term
        clubs = Club.objects.filter(name__icontains=search_term)

        # Serialize each club and add to results
        results = [serialize_club(club) for club in clubs]

    context = {"results": results}

    return JsonResponse(context)
