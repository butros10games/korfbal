"""Service helpers for player overview and stats payloads."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import (
    BooleanField,
    Count,
    Exists,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Value,
)
from django.utils import timezone

from apps.awards.models import MatchMvp
from apps.game_tracker.models import MatchData, MatchPlayer, PlayerGroup, Shot
from apps.kwt_common.utils.match_summary import build_match_summaries
from apps.player.models.player import Player
from apps.schedule.models import Season
from apps.schedule.queries.seasons import (
    default_season,
    find_season,
    season_options_payload,
)
from apps.team.models import TeamData


def resolve_season(
    season_id: str | None,
    seasons: list[Season],
) -> Season | None:
    """Resolve a requested season, defaulting to current or first available."""
    if season_id:
        return find_season(season_id, seasons)
    return default_season(seasons)


def player_seasons_queryset(player: Player) -> QuerySet[Season]:
    """Return seasons relevant to a player via indexed UNION subqueries."""
    season_ids = TeamData.objects.filter(players=player).values_list(
        "season_id",
        flat=True,
    )
    season_ids = season_ids.union(
        MatchPlayer.objects.filter(player=player).values_list(
            "match_data__match_link__season_id",
            flat=True,
        ),
        PlayerGroup.objects.filter(players=player).values_list(
            "match_data__match_link__season_id",
            flat=True,
        ),
        Shot.objects.filter(player=player).values_list(
            "match_data__match_link__season_id",
            flat=True,
        ),
    )

    return Season.objects.filter(id_uuid__in=Subquery(season_ids)).order_by(
        "-start_date"
    )


def match_queryset_for_player(
    player: Player,
    season: Season | None,
    *,
    include_roster: bool,
) -> QuerySet[MatchData]:
    """Return an optimized player-centric MatchData queryset."""
    queryset = MatchData.objects.select_related(
        "match_link",
        "match_link__home_team",
        "match_link__home_team__club",
        "match_link__away_team",
        "match_link__away_team__club",
        "match_link__season",
    )

    queryset = queryset.annotate(
        has_player_group=Exists(
            PlayerGroup.objects.filter(
                match_data=OuterRef("pk"),
                players=player,
            )
        ),
        has_shot=Exists(
            Shot.objects.filter(
                match_data=OuterRef("pk"),
                player=player,
            )
        ),
    )

    filter_q = Q(has_player_group=True) | Q(has_shot=True)

    if include_roster:
        queryset = queryset.annotate(
            is_match_roster=Exists(
                MatchPlayer.objects.filter(
                    match_data=OuterRef("pk"),
                    player=player,
                )
            ),
            is_home_teamdata_roster=Exists(
                TeamData.objects.filter(
                    team_id=OuterRef("match_link__home_team_id"),
                    season_id=OuterRef("match_link__season_id"),
                    players=player,
                )
            ),
            is_away_teamdata_roster=Exists(
                TeamData.objects.filter(
                    team_id=OuterRef("match_link__away_team_id"),
                    season_id=OuterRef("match_link__season_id"),
                    players=player,
                )
            ),
        )
        filter_q |= (
            Q(is_match_roster=True)
            | Q(is_home_teamdata_roster=True)
            | Q(is_away_teamdata_roster=True)
        )
    else:
        queryset = queryset.annotate(
            is_match_roster=Value(False, output_field=BooleanField()),
            is_home_teamdata_roster=Value(False, output_field=BooleanField()),
            is_away_teamdata_roster=Value(False, output_field=BooleanField()),
        )

    queryset = queryset.filter(filter_q)

    if season is not None:
        queryset = queryset.filter(match_link__season=season)

    return queryset


def build_player_overview_payload(
    *,
    player: Player,
    season: Season | None,
    seasons: list[Season],
) -> dict[str, Any]:
    """Build the player overview payload."""
    upcoming_matches = build_match_summaries(
        match_queryset_for_player(
            player,
            season,
            include_roster=True,
        )
        .filter(status__in=["upcoming", "active"])
        .order_by("match_link__start_time")[:10]
    )

    recent_matches = build_match_summaries(
        match_queryset_for_player(
            player,
            season,
            include_roster=False,
        )
        .filter(status="finished")
        .order_by("-match_link__start_time")[:10]
    )

    return {
        "matches": {
            "upcoming": upcoming_matches,
            "recent": recent_matches,
        },
        "seasons": season_options_payload(seasons),
        "meta": {
            "season_id": str(season.id_uuid) if season else None,
            "season_name": season.name if season else None,
        },
    }


def connected_club_recent_results(
    *,
    player: Player,
    limit: int,
    days: int | None,
    season_id: str | None,
) -> list[dict[str, Any]]:
    """Return recent finished match summaries for the player's followed clubs."""
    clubs_qs = player.club_follow.all()
    if not clubs_qs.exists():
        return []

    queryset = (
        MatchData.objects
        .select_related(
            "match_link",
            "match_link__home_team",
            "match_link__home_team__club",
            "match_link__away_team",
            "match_link__away_team__club",
            "match_link__season",
        )
        .filter(status="finished")
        .filter(
            Q(match_link__home_team__club__in=clubs_qs)
            | Q(match_link__away_team__club__in=clubs_qs)
        )
        .distinct()
    )

    if season_id:
        queryset = queryset.filter(match_link__season_id=season_id)

    if days is not None:
        cutoff = timezone.now() - timedelta(days=days)
        queryset = queryset.filter(match_link__start_time__gte=cutoff)

    return build_match_summaries(queryset.order_by("-match_link__start_time")[:limit])


def goal_type_breakdown(
    queryset: QuerySet[Shot],
    *,
    for_team: bool,
) -> list[dict[str, str | int | None]]:
    """Build a scored goal-type breakdown for shots."""
    breakdown = (
        queryset
        .filter(for_team=for_team, scored=True)
        .values("shot_type__id_uuid", "shot_type__name")
        .annotate(count=Count("id_uuid"))
        .order_by("shot_type__name")
    )

    return [
        {
            "id_uuid": str(row.get("shot_type__id_uuid"))
            if row.get("shot_type__id_uuid")
            else None,
            "name": row.get("shot_type__name") or "Onbekend",
            "count": int(row.get("count", 0)),
        }
        for row in breakdown
    ]


def build_player_stats_payload(
    *,
    player: Player,
    season: Season | None,
) -> dict[str, Any]:
    """Build the season-scoped player stats payload."""
    mvp_queryset = MatchMvp.objects.filter(
        mvp_player=player,
        published_at__isnull=False,
    )
    if season is not None:
        mvp_queryset = mvp_queryset.filter(match__season=season)

    mvp_match_ids = list(mvp_queryset.values_list("match_id", flat=True))
    mvp_matches: list[dict[str, Any]] = []
    if mvp_match_ids:
        mvp_matchdata_queryset = (
            MatchData.objects
            .select_related(
                "match_link",
                "match_link__home_team",
                "match_link__home_team__club",
                "match_link__away_team",
                "match_link__away_team__club",
                "match_link__season",
            )
            .filter(
                status="finished",
                match_link_id__in=mvp_match_ids,
            )
            .distinct()
        )
        mvp_matches = build_match_summaries(
            mvp_matchdata_queryset.order_by("-match_link__start_time")
        )

    shot_queryset = Shot.objects.select_related("match_data", "shot_type").filter(
        player=player
    )
    if season is not None:
        shot_queryset = shot_queryset.filter(match_data__match_link__season=season)

    aggregated = shot_queryset.aggregate(
        shots_for=Count("id_uuid", filter=Q(for_team=True)),
        shots_against=Count("id_uuid", filter=Q(for_team=False)),
        goals_for=Count("id_uuid", filter=Q(for_team=True, scored=True)),
        goals_against=Count("id_uuid", filter=Q(for_team=False, scored=True)),
    )

    return {
        "shots_for": int(aggregated.get("shots_for", 0)),
        "shots_against": int(aggregated.get("shots_against", 0)),
        "goals_for": int(aggregated.get("goals_for", 0)),
        "goals_against": int(aggregated.get("goals_against", 0)),
        "mvps": int(mvp_queryset.count()),
        "mvp_matches": mvp_matches,
        "goal_types": {
            "for": goal_type_breakdown(shot_queryset, for_team=True),
            "against": goal_type_breakdown(shot_queryset, for_team=False),
        },
    }
