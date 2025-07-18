"""View for displaying a match's details."""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render

from apps.common.utils import get_time_display
from apps.game_tracker.models import MatchData, Shot
from apps.schedule.models import Match


def match_detail(request: HttpRequest, match_id: str) -> HttpResponse:
    """Render the match detail page.

    Args:
        request: The request object.
        match_id: The id of the match.

    Returns:
        The rendered match detail page.

    """
    match_model: Match = get_object_or_404(Match, id_uuid=match_id)

    match_data: MatchData = MatchData.objects.get(match_link=match_model)

    home_score: int = Shot.objects.filter(
        match_data=match_data, team=match_model.home_team, scored=True,
    ).count()

    away_score: int = Shot.objects.filter(
        match_data=match_data, team=match_model.away_team, scored=True,
    ).count()

    context: dict = {
        "match": match_model,
        "match_data": match_data,
        "time_display": get_time_display(match_data),
        "start_date": match_model.start_time.strftime("%A, %d %B"),
        "start_time": match_model.start_time.strftime("%H:%M"),
        "home_score": home_score,
        "away_score": away_score,
    }

    return render(request, "matches/detail.html", context)
