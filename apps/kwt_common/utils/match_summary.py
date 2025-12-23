"""Helpers to transform match data objects into API friendly dictionaries."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast
from uuid import UUID

from django.utils.timezone import localtime

from apps.game_tracker.models import MatchData
from apps.game_tracker.services.match_scores import compute_scores_for_matchdata_ids
from apps.kwt_common.utils.time_utils import get_time_display


MatchSummary = dict[str, Any]


def build_match_summaries(match_data: Iterable[MatchData]) -> list[MatchSummary]:
    """Serialize match data rows into a lightweight summary payload.

    Returns:
        list[MatchSummary]: List of match summaries.

    """
    entries = list(match_data)

    # Active matches should show the current score, which is derived from shots.
    active_match_data_ids: list[UUID] = [
        cast(UUID, entry.id_uuid) for entry in entries if entry.status == "active"
    ]
    active_scores = compute_scores_for_matchdata_ids(active_match_data_ids)

    summaries: list[MatchSummary] = []
    for entry in entries:
        match = entry.match_link
        home_team = match.home_team
        away_team = match.away_team

        entry_uuid = cast(UUID, entry.id_uuid)

        if entry.status == "active":
            computed = active_scores.get(entry_uuid)
            if computed is not None:
                home_score, away_score = computed
            else:
                home_score = 0
                away_score = 0
        else:
            # Upcoming + finished matches use the persisted scores.
            home_score = int(getattr(entry, "home_score", 0) or 0)
            away_score = int(getattr(entry, "away_score", 0) or 0)

        summaries.append({
            "id_uuid": str(match.id_uuid),
            "match_data_id": str(entry_uuid),
            "start_time": localtime(match.start_time).isoformat(),
            "status": entry.status,
            "competition": match.season.name,
            "location": home_team.club.name,
            "match_url": match.get_absolute_url(),
            "score": {
                "home": home_score,
                "away": away_score,
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
