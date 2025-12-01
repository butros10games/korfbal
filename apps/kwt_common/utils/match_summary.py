"""Helpers to transform match data objects into API friendly dictionaries."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.utils.timezone import localtime

from apps.game_tracker.models import MatchData
from apps.kwt_common.utils.time_utils import get_time_display


MatchSummary = dict[str, Any]


def build_match_summaries(match_data: Iterable[MatchData]) -> list[MatchSummary]:
    """Serialize match data rows into a lightweight summary payload.

    Returns:
        list[MatchSummary]: List of match summaries.

    """
    summaries: list[MatchSummary] = []
    for entry in match_data:
        match = entry.match_link
        home_team = match.home_team
        away_team = match.away_team

        summaries.append({
            "id_uuid": str(match.id_uuid),
            "match_data_id": str(entry.id_uuid),
            "start_time": localtime(match.start_time).isoformat(),
            "status": entry.status,
            "competition": match.season.name,
            "location": home_team.club.name,
            "match_url": match.get_absolute_url(),
            "score": {
                "home": entry.home_score,
                "away": entry.away_score,
            },
            "home": {
                "name": home_team.name,
                "club": home_team.club.name,
                "logo_url": home_team.club.get_club_logo(),
            },
            "away": {
                "name": away_team.name,
                "club": away_team.club.name,
                "logo_url": away_team.club.get_club_logo(),
            },
            "current_part": entry.current_part,
            "parts": entry.parts,
            "time_display": get_time_display(entry),
        })

    return summaries
