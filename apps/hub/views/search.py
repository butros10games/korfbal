"""Search view for searching teams and clubs."""

from django.db.models import F, Value
from django.db.models.functions import Concat
from django.http import HttpRequest, JsonResponse
from django.utils import timezone

from apps.club.models import Club
from apps.schedule.models import Season
from apps.team.models import Team, TeamData


def get_current_season() -> Season | None:
    """Get the current season.

    Returns:
        Season: The current season.

    """
    today = timezone.now().date()
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


def serialize_team(team: Team, current_season: Season) -> dict[str, str | None]:
    """Serialize a team.

    Args:
        team (Team): The team to serialize.
        current_season (Season): The current season.

    Returns:
        dict: The serialized team.

    """
    team_data = TeamData.objects.filter(team=team, season=current_season).first()
    return {
        "id": str(team.id_uuid),
        "name": str(team),
        "img_url": team.club.get_club_logo(),
        "competition": team_data.competition if team_data else "",
        "url": team.get_absolute_url(),
    }


def serialize_club(club: Club) -> dict[str, str | None]:
    """Serialize a club.

    Args:
        club (Club): The club to serialize.

    Returns:
        dict: The serialized club.

    """
    return {
        "id": str(club.id_uuid),
        "name": club.name,
        "img_url": club.get_club_logo(),
        "competition": None,
        "url": club.get_absolute_url(),
    }


def search(request: HttpRequest) -> JsonResponse:
    """Search view for searching teams and clubs.

    Args:
        request (HttpRequest): The request object.

    Returns:
        JsonResponse: The JSON response object.

    """
    search_term = request.GET.get("q", "")
    category = request.GET.get("category", "")

    results: list[dict[str, str | None]] = []

    if category == "teams":
        current_season = get_current_season()

        if not current_season:
            return JsonResponse({"results": results})

        # Annotate teams with full name (club name + team name) and
        # filter by search term
        teams = Team.objects.annotate(
            full_name=Concat(F("club__name"), Value(" "), F("name")),
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
