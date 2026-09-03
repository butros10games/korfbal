"""Build the complete public tournament presentation payload."""

from __future__ import annotations

from typing import Any

from apps.tournament.models import Tournament, TournamentMatch, TournamentPool
from apps.tournament.services.standings import calculate_pool_standings


def _qualifier_label(
    source: dict[str, Any], pools_by_id: dict[str, TournamentPool]
) -> str | None:
    if not source:
        return None
    pool_ids = source.get("pool_ids")
    rank = source.get("rank")
    if not isinstance(pool_ids, list) or not isinstance(rank, int):
        return None
    source_pools = [pools_by_id.get(str(pool_id)) for pool_id in pool_ids]
    if any(pool is None for pool in source_pools):
        return None
    names = [pool.name for pool in source_pools if pool is not None]
    if source.get("kind") == "pool_rank" and len(names) == 1:
        return f"{names[0]} #{rank}"
    if source.get("kind") == "best_rank":
        return f"Beste #{rank} van {', '.join(names)}"
    return None


def build_tournament_snapshot(tournament: Tournament) -> dict[str, Any]:
    """Return one consistent payload for public pages and Cast receivers."""
    config = tournament.display_config
    pools = list(
        tournament.pools.select_related("stage", "assigned_field").prefetch_related(
            "entries__team",
            "entries__adjustments",
            "matches",
        )
    )
    matches = list(
        tournament.matches.select_related(
            "stage",
            "stage__final_group",
            "pool",
            "field",
            "home_team",
            "away_team",
            "winner",
        )
    )
    pools_by_id = {str(pool.id_uuid): pool for pool in pools}
    winner_sources = {
        (str(match.next_match_id), match.winner_to_side): (
            f"Winnaar {match.stage.name} · wedstrijd {match.match_number}"
        )
        for match in matches
        if match.next_match_id and match.winner_to_side
    }
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
                "assigned_field": (
                    {
                        "id_uuid": str(pool.assigned_field_id),
                        "label": pool.assigned_field.label,
                    }
                    if pool.assigned_field
                    else None
                ),
                "standings": calculate_pool_standings(pool),
            }
            for pool in pools
        ],
        "final_groups": [
            {
                "id_uuid": str(group.id_uuid),
                "name": group.name,
                "format": group.format,
                "sort_order": group.sort_order,
            }
            for group in tournament.final_groups.all()
        ],
        "matches": [
            {
                "id_uuid": str(match.id_uuid),
                "stage_id": str(match.stage_id),
                "stage_name": match.stage.name,
                "stage_kind": match.stage.kind,
                "final_group_id": (
                    str(match.stage.final_group_id)
                    if match.stage.final_group_id
                    else None
                ),
                "final_group_name": (
                    match.stage.final_group.name if match.stage.final_group else None
                ),
                "pool_id": str(match.pool_id) if match.pool_id else None,
                "pool_name": match.pool.name if match.pool else None,
                "home_team": (
                    {
                        "id_uuid": str(match.home_team_id),
                        "name": match.home_team.name,
                        "short_name": match.home_team.short_name,
                        "color": match.home_team.color,
                    }
                    if match.home_team
                    else None
                ),
                "away_team": (
                    {
                        "id_uuid": str(match.away_team_id),
                        "name": match.away_team.name,
                        "short_name": match.away_team.short_name,
                        "color": match.away_team.color,
                    }
                    if match.away_team
                    else None
                ),
                "home_source_label": _qualifier_label(match.home_qualifier, pools_by_id)
                or winner_sources.get((
                    str(match.id_uuid),
                    TournamentMatch.DestinationSide.HOME,
                )),
                "away_source_label": _qualifier_label(match.away_qualifier, pools_by_id)
                or winner_sources.get((
                    str(match.id_uuid),
                    TournamentMatch.DestinationSide.AWAY,
                )),
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
