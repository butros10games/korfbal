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
    MatchImpactRow,
    PlayerImpactBreakdown,
    compute_match_impact_breakdown,
    compute_match_impact_rows,
)


def _load_impact_dependencies(
    rows: list[MatchImpactRow],
) -> tuple[dict[str, Player], dict[str, Team]]:
    """Load the player and team records shared by both persistence paths."""
    players_by_id = {
        str(player.id_uuid): player
        for player in Player.objects.filter(
            id_uuid__in={row.player_id for row in rows},
        ).only("id_uuid")
    }
    teams_by_id = {
        str(team.id_uuid): team
        for team in Team.objects.filter(
            id_uuid__in={row.team_id for row in rows if row.team_id},
        ).only("id_uuid")
    }
    return players_by_id, teams_by_id


def _upsert_impact_row(
    *,
    match_data: MatchData,
    row: MatchImpactRow,
    algorithm_version: str,
    players_by_id: dict[str, Player],
    teams_by_id: dict[str, Team],
) -> PlayerMatchImpact | None:
    """Persist one score row, skipping rows whose player no longer exists."""
    player = players_by_id.get(row.player_id)
    if not player:
        return None

    impact, _created = PlayerMatchImpact.objects.update_or_create(
        match_data=match_data,
        player=player,
        defaults={
            "team": teams_by_id.get(row.team_id) if row.team_id else None,
            "impact_score": row.impact_score,
            "algorithm_version": algorithm_version,
        },
    )
    return impact


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

    players_by_id, teams_by_id = _load_impact_dependencies(rows)

    upserted = 0
    with transaction.atomic():
        for row in rows:
            impact = _upsert_impact_row(
                match_data=match_data,
                row=row,
                algorithm_version=algorithm_version,
                players_by_id=players_by_id,
                teams_by_id=teams_by_id,
            )
            if impact is not None:
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

    players_by_id, teams_by_id = _load_impact_dependencies(rows)

    upserted = 0
    with transaction.atomic():
        for row in rows:
            impact_obj = _upsert_impact_row(
                match_data=match_data,
                row=row,
                algorithm_version=algorithm_version,
                players_by_id=players_by_id,
                teams_by_id=teams_by_id,
            )
            if impact_obj is None:
                continue

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
