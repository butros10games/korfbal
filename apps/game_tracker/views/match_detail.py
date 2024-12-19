"""View for displaying a match's details."""

from django.shortcuts import get_object_or_404, render

from apps.game_tracker.models import MatchData, Shot
from apps.schedule.models import Match

from .common import get_time_display


def match_detail(request, match_id):
    """
    Render the match detail page.

    Args:
        request: The request object.
        match_id: The id of the match.

    Returns:
        The rendered match detail page.
    """
    match_model = get_object_or_404(Match, id_uuid=match_id)

    match_data = MatchData.objects.get(match_link=match_model)

    context = {
        "match": match_model,
        "match_data": match_data,
        "time_display": get_time_display(match_data),
        "start_date": match_model.start_time.strftime("%A, %d %B"),
        "start_time": match_model.start_time.strftime("%H:%M"),
        "home_score": Shot.objects.filter(
            match_data=match_data, team=match_model.home_team, scored=True
        ).count(),
        "away_score": Shot.objects.filter(
            match_data=match_data, team=match_model.away_team, scored=True
        ).count(),
    }

    return render(request, "matches/detail.html", context)
