"""Build the complete public tournament presentation payload."""

from __future__ import annotations

from typing import Any

from apps.tournament.models import Tournament
from apps.tournament.services.standings import calculate_pool_standings


def build_tournament_snapshot(tournament: Tournament) -> dict[str, Any]:
    """Return one consistent payload for public pages and Cast receivers."""
    config = tournament.display_config
    pools = list(
        tournament.pools.select_related("stage").prefetch_related(
            "entries__team",
            "entries__adjustments",
            "matches",
        )
    )
    matches = list(
        tournament.matches.select_related(
            "stage", "pool", "field", "home_team", "away_team", "winner"
        )
    )
    return {
        "tournament": {
            "id_uuid": str(tournament.id_uuid),
            "name": tournament.name,
            "slug": tournament.slug,
            "location": tournament.location,
            "timezone": tournament.timezone,
            "starts_at": tournament.starts_at.isoformat(),
            "ends_at": tournament.ends_at.isoformat() if tournament.ends_at else None,
            "status": tournament.status,
            "win_points": tournament.win_points,
            "draw_points": tournament.draw_points,
            "loss_points": tournament.loss_points,
            "match_duration_minutes": tournament.match_duration_minutes,
            "changeover_minutes": tournament.changeover_minutes,
            "minimum_rest_minutes": tournament.minimum_rest_minutes,
            "live_revision": tournament.live_revision,
            "live_changed_at": tournament.live_changed_at.isoformat(),
        },
        "display": {
            "rotation_seconds": config.rotation_seconds,
            "show_live": config.show_live,
            "show_standings": config.show_standings,
            "show_upcoming": config.show_upcoming,
            "show_recent": config.show_recent,
            "accent_color": config.accent_color,
            "announcement": config.announcement,
        },
        "fields": [
            {
                "id_uuid": str(field.id_uuid),
                "label": field.label,
                "sort_order": field.sort_order,
            }
            for field in tournament.fields.filter(active=True)
        ],
        "teams": [
            {
                "id_uuid": str(team.id_uuid),
                "name": team.name,
                "short_name": team.short_name,
                "affiliation": team.affiliation,
                "seed": team.seed,
                "sort_order": team.sort_order,
                "color": team.color,
                "checked_in": team.checked_in,
                "withdrawn": team.withdrawn,
            }
            for team in tournament.teams.all()
        ],
        "pools": [
            {
                "id_uuid": str(pool.id_uuid),
                "name": pool.name,
                "stage_name": pool.stage.name,
                "standings": calculate_pool_standings(pool),
            }
            for pool in pools
        ],
        "matches": [
            {
                "id_uuid": str(match.id_uuid),
                "stage_id": str(match.stage_id),
                "stage_name": match.stage.name,
                "stage_kind": match.stage.kind,
                "pool_id": str(match.pool_id) if match.pool_id else None,
                "pool_name": match.pool.name if match.pool else None,
                "home_team": (
                    {
                        "id_uuid": str(match.home_team_id),
                        "name": match.home_team.name,
                        "short_name": match.home_team.short_name,
                    }
                    if match.home_team
                    else None
                ),
                "away_team": (
                    {
                        "id_uuid": str(match.away_team_id),
                        "name": match.away_team.name,
                        "short_name": match.away_team.short_name,
                    }
                    if match.away_team
                    else None
                ),
                "field": (
                    {"id_uuid": str(match.field_id), "label": match.field.label}
                    if match.field
                    else None
                ),
                "round_number": match.round_number,
                "match_number": match.match_number,
                "starts_at": match.starts_at.isoformat() if match.starts_at else None,
                "duration_minutes": match.duration_minutes,
                "status": match.status,
                "field_ready_at": (
                    match.field_ready_at.isoformat() if match.field_ready_at else None
                ),
                "home_score": match.home_score,
                "away_score": match.away_score,
                "winner_id": str(match.winner_id) if match.winner_id else None,
                "revision": match.revision,
            }
            for match in matches
        ],
    }
