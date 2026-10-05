"""Service helpers for player overview and stats payloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.db.models import (
    Count,
    F,
    Q,
    QuerySet,
    Subquery,
)
from django.utils import timezone

from apps.awards.models import MatchMvp
from apps.game_tracker.application.ports import SourceResultReader
from apps.game_tracker.models import MatchData, MatchPlayer, PlayerGroup, Shot
from apps.game_tracker.queries.match_summaries import build_match_summaries
from apps.player.models.player import Player
from apps.schedule.models import Season
from apps.schedule.queries.seasons import (
    default_season,
    find_season,
    season_options_payload,
)
from apps.team.models import TeamData


# Explicit request for every season of a player's career.
CAREER = "career"
DEFAULT_SCOPE = "default"
EXPLICIT_SCOPE = "explicit"
INVALID_SEASON = "invalid_season"


@dataclass(frozen=True)
class SeasonSelection:
    """A resolved player season scope; ``season`` None means the whole career."""

    season: Season | None
    scope: str

    def as_meta(self) -> dict[str, Any]:
        """Describe the requested and resolved scope for clients and caches."""
        return {
            "season_id": str(self.season.id_uuid) if self.season else None,
            "season_name": self.season.name if self.season else None,
            "season_scope": self.scope,
        }


class InvalidSeasonError(ValueError):
    """An explicit season request outside the player's own seasons."""


def resolve_season(season_id: str | None, seasons: list[Season]) -> SeasonSelection:
    """Resolve a player's season request without ever widening it silently.

    An omitted season resolves the player's current (or most recent) season;
    ``career`` explicitly selects every season. A malformed, unknown or
    out-of-scope season is an error, never a career-wide fallback.

    Returns:
        The selected season and how it was chosen.

    Raises:
        InvalidSeasonError: The explicit season is not one of the player's.

    """
    season_id = (season_id or "").strip()
    if not season_id:
        default = default_season(seasons)
        return SeasonSelection(default, DEFAULT_SCOPE if default else CAREER)
    if season_id == CAREER:
        return SeasonSelection(None, CAREER)
    season = find_season(season_id, seasons)
    if season is None:
        raise InvalidSeasonError(season_id)
    return SeasonSelection(season, EXPLICIT_SCOPE)


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

    # Start with the player's indexed participation records instead of probing
    # every imported match once for each possible kind of participation.
    match_ids = (
        PlayerGroup.objects
        .filter(players=player)
        .values_list("match_data_id", flat=True)
        .union(
            Shot.objects.filter(player=player).values_list("match_data_id", flat=True)
        )
    )

    if include_roster:
        roster = TeamData.objects.filter(players=player)
        match_ids = match_ids.union(
            MatchPlayer.objects.filter(player=player).values_list(
                "match_data_id", flat=True
            ),
            roster.filter(team__home_matches__season_id=F("season_id")).values_list(
                "team__home_matches__tracker_data__pk", flat=True
            ),
            roster.filter(team__away_matches__season_id=F("season_id")).values_list(
                "team__away_matches__tracker_data__pk", flat=True
            ),
        )

    queryset = queryset.filter(pk__in=Subquery(match_ids))

    if season is not None:
        queryset = queryset.filter(match_link__season=season)

    return queryset


def build_player_overview_payload(
    *,
    player: Player,
    season: Season | None,
    seasons: list[Season],
    selection: SeasonSelection | None = None,
    source_results: SourceResultReader | None = None,
) -> dict[str, Any]:
    """Build the player overview payload."""
    selection = selection or SeasonSelection(
        season, EXPLICIT_SCOPE if season else CAREER
    )
    upcoming_matches = build_match_summaries(
        match_queryset_for_player(
            player,
            season,
            include_roster=True,
        )
        .filter(status__in=["upcoming", "active"])
        .order_by("match_link__start_time")[:10],
        source_results=source_results,
    )

    recent_matches = build_match_summaries(
        match_queryset_for_player(
            player,
            season,
            include_roster=False,
        )
        .filter(status="finished")
        .order_by("-match_link__start_time")[:10],
        source_results=source_results,
    )

    return {
        "matches": {
            "upcoming": upcoming_matches,
            "recent": recent_matches,
        },
        "seasons": season_options_payload(seasons),
        "meta": selection.as_meta(),
    }


def connected_club_recent_results(
    *,
    player: Player,
    limit: int,
    days: int | None,
    season_id: str | None,
    source_results: SourceResultReader | None = None,
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

    return build_match_summaries(
        queryset.order_by("-match_link__start_time")[:limit],
        source_results=source_results,
    )


def _shot_stats(queryset: QuerySet[Shot]) -> dict[str, Any]:
    """Build totals and goal breakdowns from one grouped scan of player shots."""
    rows = (
        queryset
        .values("for_team", "shot_type__id_uuid", "shot_type__name")
        .annotate(shots=Count("pk"), goals=Count("pk", filter=Q(scored=True)))
        .order_by("shot_type__name")
    )
    totals = dict.fromkeys(
        ("shots_for", "shots_against", "goals_for", "goals_against"), 0
    )
    breakdown: dict[str, list[dict[str, str | int | None]]] = {
        "for": [],
        "against": [],
    }
    for row in rows:
        side = "for" if row["for_team"] else "against"
        totals[f"shots_{side}"] += row["shots"]
        totals[f"goals_{side}"] += row["goals"]
        if row["goals"]:
            breakdown[side].append({
                "id_uuid": str(row["shot_type__id_uuid"])
                if row["shot_type__id_uuid"]
                else None,
                "name": row["shot_type__name"] or "Onbekend",
                "count": row["goals"],
            })
    return {**totals, "goal_types": breakdown}


def build_player_stats_payload(
    *,
    player: Player,
    season: Season | None,
    selection: SeasonSelection | None = None,
    source_results: SourceResultReader | None = None,
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
            mvp_matchdata_queryset.order_by("-match_link__start_time"),
            source_results=source_results,
        )

    shot_queryset = Shot.objects.select_related("match_data", "shot_type").filter(
        player=player
    )
    if season is not None:
        shot_queryset = shot_queryset.filter(match_data__match_link__season=season)

    return {
        **_shot_stats(shot_queryset),
        "mvps": len(mvp_match_ids),
        "mvp_matches": mvp_matches,
        "meta": (
            selection or SeasonSelection(season, EXPLICIT_SCOPE if season else CAREER)
        ).as_meta(),
    }
