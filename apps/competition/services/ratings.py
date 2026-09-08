"""Cached read model rebuilt from the current corrected result history."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any
from uuid import UUID

from django.core.cache import cache
from django.db.models import Count, Max
from django.utils import timezone

from apps.competition.domain.elo import (
    MODEL_VERSION,
    PROVISIONAL_GAMES,
    RatedResult,
    calculate,
)
from apps.competition.models import Match, Team


CACHE_SECONDS = 300


def team_ratings(season_id: UUID) -> dict[str, Any]:
    """Return a provisional Elo view, invalidating cached scores on result changes."""
    teams = list(
        Team.objects.filter(season_id=season_id).values(
            "id", "external_id", "name", "sport", "club_id"
        )
    )
    matches = Match.objects.filter(season_id=season_id)
    fingerprint = matches.aggregate(count=Count("pk"), changed=Max("updated_at"))
    population = sha256(
        json.dumps(sorted((team["id"], team["sport"]) for team in teams)).encode()
    ).hexdigest()[:16]
    changed = fingerprint["changed"]
    stamp = changed.isoformat() if changed else "empty"
    key = (
        f"competition:{MODEL_VERSION}:{season_id}:"
        f"{fingerprint['count']}:{stamp}:{population}"
    )
    cached = cache.get(key)
    if cached is None:
        results = [
            RatedResult(
                source_id=row["external_id"],
                starts_at=row["starts_at"],
                home=row["home_team_id"],
                away=row["away_team_id"],
                home_score=row["home_score"],
                away_score=row["away_score"],
            )
            for row in matches.filter(
                status="FINAL",
                automatic_result=False,
                home_score__isnull=False,
                away_score__isnull=False,
                result_observed_at__isnull=False,
            ).values(
                "external_id",
                "starts_at",
                "home_team_id",
                "away_team_id",
                "home_score",
                "away_score",
            )
        ]
        scores = calculate({team["id"]: team["sport"] for team in teams}, results)
        cached = (scores, timezone.now().isoformat())
        cache.set(key, cached, CACHE_SECONDS)
    scores, computed_at = cached
    rows: list[dict[str, Any]] = [
        {
            **team,
            "rating": round(scores[team["id"]].value, 2),
            "games": scores[team["id"]].games,
            "provisional": scores[team["id"]].games < PROVISIONAL_GAMES,
            "comparison_group": (
                f"{team['sport']}:{scores[team['id']].comparison_group}"
            ),
        }
        for team in teams
    ]
    return {
        "model": MODEL_VERSION,
        "computed_at": computed_at,
        "results": sorted(
            rows,
            key=lambda row: (
                row["comparison_group"],
                -row["rating"],
                row["external_id"],
            ),
        ),
    }
