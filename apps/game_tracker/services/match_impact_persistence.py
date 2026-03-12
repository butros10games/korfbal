"""Persistence and caching helpers for match impact scoring."""

from __future__ import annotations

import contextlib

from django.core.cache import cache
from django.db import transaction

from apps.game_tracker.models import (
    MatchData,
    PlayerMatchImpact,
    PlayerMatchImpactBreakdown,
)
from apps.player.models.player import Player
from apps.team.models.team import Team

from .match_impact_scorer import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    MATCH_IMPACT_BREAKDOWN_CACHE_VERSION,
    PlayerImpactBreakdown,
    compute_match_impact_breakdown,
    compute_match_impact_rows,
)


def compute_match_impact_breakdown_cached(
    *,
    match_data: MatchData,
    algorithm_version: str = LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    timeout_seconds: int = 60 * 60 * 24,
) -> PlayerImpactBreakdown:
    """Return cached per-match breakdown (diagnostics)."""
    cache_key = (
        "match-impact-breakdown:"
        f"v{MATCH_IMPACT_BREAKDOWN_CACHE_VERSION}:"
        f"{algorithm_version}:{match_data.id_uuid}"
    )

    try:
        cached = cache.get(cache_key)
    except Exception:  # noqa: BLE001
        cached = None

    if isinstance(cached, dict):
        return cached

    _rows, breakdown = compute_match_impact_breakdown(
        match_data=match_data,
        algorithm_version=algorithm_version,
    )

    with contextlib.suppress(Exception):
        cache.set(cache_key, breakdown, timeout=timeout_seconds)
    return breakdown


def persist_match_impact_rows(
    *,
    match_data: MatchData,
    algorithm_version: str = LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
) -> int:
    """Compute + upsert rows for a match."""
    rows = compute_match_impact_rows(
        match_data=match_data,
        algorithm_version=algorithm_version,
    )
    if not rows:
        return 0

    players_by_id: dict[str, Player] = {
        str(p.id_uuid): p
        for p in Player.objects.filter(id_uuid__in=[r.player_id for r in rows]).only(
            "id_uuid"
        )
    }

    team_ids = [r.team_id for r in rows if r.team_id]
    teams_by_id: dict[str, Team] = {
        str(t.id_uuid): t
        for t in Team.objects.filter(id_uuid__in=team_ids).only("id_uuid")
    }

    upserted = 0
    with transaction.atomic():
        for row in rows:
            player = players_by_id.get(row.player_id)
            if not player:
                continue

            team = teams_by_id.get(row.team_id) if row.team_id else None

            PlayerMatchImpact.objects.update_or_create(
                match_data=match_data,
                player=player,
                defaults={
                    "team": team,
                    "impact_score": row.impact_score,
                    "algorithm_version": algorithm_version,
                },
            )
            upserted += 1

    return upserted


def persist_match_impact_rows_with_breakdowns(
    *,
    match_data: MatchData,
    algorithm_version: str = LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
) -> int:
    """Compute + upsert impact rows and per-player breakdown rows for a match."""
    rows, breakdown_by_player = compute_match_impact_breakdown(
        match_data=match_data,
        algorithm_version=algorithm_version,
    )
    if not rows:
        return 0

    players_by_id: dict[str, Player] = {
        str(p.id_uuid): p
        for p in Player.objects.filter(id_uuid__in=[r.player_id for r in rows]).only(
            "id_uuid"
        )
    }

    team_ids = [r.team_id for r in rows if r.team_id]
    teams_by_id: dict[str, Team] = {
        str(t.id_uuid): t
        for t in Team.objects.filter(id_uuid__in=team_ids).only("id_uuid")
    }

    upserted = 0
    with transaction.atomic():
        for row in rows:
            player = players_by_id.get(row.player_id)
            if not player:
                continue

            team = teams_by_id.get(row.team_id) if row.team_id else None

            impact_obj, _created = PlayerMatchImpact.objects.update_or_create(
                match_data=match_data,
                player=player,
                defaults={
                    "team": team,
                    "impact_score": row.impact_score,
                    "algorithm_version": algorithm_version,
                },
            )

            per_player_breakdown = breakdown_by_player.get(row.player_id) or {}

            PlayerMatchImpactBreakdown.objects.update_or_create(
                impact=impact_obj,
                defaults={
                    "algorithm_version": algorithm_version,
                    "breakdown": per_player_breakdown,
                },
            )
            upserted += 1

    return upserted
