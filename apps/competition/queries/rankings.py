"""Explicit ORM read boundary for public club-team Elo rankings."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone

from apps.competition.domain.team_elo import PROVISIONAL_GAMES
from apps.competition.models import TeamRating


ACTIVE_DAYS = 400
CONTEXT_FILTERS = {
    "gender": "competition_class__edition__gender",
    "category": "competition_class__category",
    "team_kind": "competition_class__team_kind",
    "colour": "competition_class__colour",
    "playing_format": "competition_class__playing_format",
    "class_code": "competition_class__code",
    "discipline": "competition_class__edition__discipline",
}


def rankings(filters: dict[str, Any]) -> list[TeamRating]:
    """Rank active club teams of one age group by their latest classification.

    Ranks are assigned to the whole selection before club and name filters, so a
    searched team keeps its position; equal ratings share a rank.
    """
    since = timezone.now() - timedelta(days=filters.get("active_days", ACTIVE_DAYS))
    query = TeamRating.objects.filter(
        last_played_at__gte=since,
        competition_class__age_group=filters["age_group"],
    )
    for name, lookup in CONTEXT_FILTERS.items():
        if name in filters:
            query = query.filter(**{lookup: filters[name]})
    ranked = list(
        query.select_related("team__club", "competition_class__edition").order_by(
            "-rating", "team_id"
        )
    )
    previous, rank = None, 0
    for position, rating in enumerate(ranked, start=1):
        if rating.rating != previous:
            previous, rank = rating.rating, position
        rating.rank = rank
    search = filters.get("search", "").casefold()
    return [
        rating
        for rating in ranked
        if ("club" not in filters or rating.team.club_id == filters["club"])
        # Team names are often just "1"; readers search for "Fortuna 1".
        and search in f"{rating.team.club.name} {rating.team.name}".casefold()
    ]


def ranking_row(rating: TeamRating) -> dict[str, Any]:
    """Serialize one ranked team with its latest competition context."""
    context = rating.competition_class
    assert context is not None
    return {
        "rank": rating.rank,
        "team": str(rating.team_id),
        "team_name": rating.team.name,
        "club": str(rating.team.club_id),
        "club_name": rating.team.club.name,
        "rating": round(rating.rating, 1),
        "phase_change": round(rating.rating - rating.phase_start, 1),
        "games": rating.games,
        "provisional": rating.games < PROVISIONAL_GAMES,
        "last_played_at": rating.last_played_at,
        "comparison_group": rating.comparison_group,
        "discipline": context.edition.discipline,
        "phase": context.edition.phase,
        "gender": context.edition.gender,
        "category": context.category,
        "age_group": context.age_group,
        "colour": context.colour,
        "playing_format": context.playing_format,
        "team_kind": context.team_kind,
        "class_code": context.code,
        "class_level": context.level,
    }
