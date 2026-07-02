"""Team overview payload construction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from asgiref.sync import async_to_sync
from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.game_tracker.models import MatchData, MatchPlayer, Shot
from apps.kwt_common.utils.general_stats import build_general_stats
from apps.kwt_common.utils.match_summary import build_match_summaries
from apps.kwt_common.utils.players_stats import build_player_stats
from apps.player.models import Player
from apps.player.privacy import can_view_by_visibility
from apps.schedule.models import Season
from apps.team.models.team import Team
from apps.team.models.team_data import TeamData


@dataclass(frozen=True, slots=True)
class TeamOverviewOptions:
    """Options and adapter-provided values for a team overview payload."""

    include_stats: bool
    include_roster: bool
    viewer_player: Player | None
    viewer_can_manage_goal_songs: bool
    fallback_goal_song_audio_urls: list[str]
    team_payload: Mapping[str, object]


def build_team_overview_payload(
    *,
    team: Team,
    season: Season | None,
    seasons: list[Season],
    options: TeamOverviewOptions,
) -> dict[str, Any]:
    """Build the stable API payload for the team overview endpoint."""
    match_data_qs = _team_match_queryset(team, season)
    upcoming_matches = build_match_summaries(
        match_data_qs.filter(status__in=["upcoming", "active"]).order_by(
            "match_link__start_time",
        )[:10],
    )
    recent_matches = build_match_summaries(
        match_data_qs.filter(status="finished").order_by("-match_link__start_time")[
            :10
        ],
    )

    stats_general = None
    if options.include_stats and match_data_qs.exists():
        stats_general = async_to_sync(build_general_stats)(match_data_qs)

    roster_players: list[Player] = []
    if options.include_roster or options.include_stats:
        roster_players = list(_team_players_queryset(team, season, match_data_qs))

    main_roster_ids = _main_roster_ids(team=team, season=season)
    ordered_roster_players = _order_roster_players(
        roster_players=roster_players,
        main_roster_ids=main_roster_ids,
    )

    roster: list[dict[str, str]] = []
    if options.include_roster:
        roster = [
            {
                "id_uuid": str(player.id_uuid),
                "display_name": player.user.username,
                "username": player.user.username,
                "roster_role": (
                    "main" if str(player.id_uuid) in main_roster_ids else "reserve"
                ),
                "profile_picture_url": (
                    player.get_profile_picture()
                    if can_view_by_visibility(
                        visibility=player.profile_picture_visibility,
                        viewer=options.viewer_player,
                        target=player,
                    )
                    else player.get_placeholder_profile_picture_url()
                ),
                "profile_url": player.get_absolute_url(),
            }
            for player in ordered_roster_players
        ]

    stats_players = []
    if options.include_stats and roster_players and match_data_qs.exists():
        stats_players = async_to_sync(build_player_stats)(roster_players, match_data_qs)

    current_season = _current_season()
    seasons_payload = [
        {
            "id_uuid": str(option.id_uuid),
            "name": option.name,
            "start_date": option.start_date.isoformat(),
            "end_date": option.end_date.isoformat(),
            "is_current": current_season is not None
            and option.id_uuid == current_season.id_uuid,
        }
        for option in seasons
    ]

    return {
        "team": options.team_payload,
        "matches": {
            "upcoming": upcoming_matches,
            "recent": recent_matches,
        },
        "stats": {
            "general": stats_general,
            "players": stats_players,
        },
        "roster": roster,
        "seasons": seasons_payload,
        "meta": {
            "season_id": str(season.id_uuid) if season else None,
            "season_name": season.name if season else None,
            "roster_count": len(roster),
            "viewer_can_manage_goal_songs": options.viewer_can_manage_goal_songs,
            "fallback_goal_song_audio_urls": options.fallback_goal_song_audio_urls,
        },
    }


def _current_season() -> Season | None:
    today = timezone.now().date()
    return Season.objects.filter(
        start_date__lte=today,
        end_date__gte=today,
    ).first()


def _team_match_queryset(
    team: Team,
    season: Season | None,
) -> QuerySet[MatchData]:
    queryset = MatchData.objects.select_related(
        "match_link",
        "match_link__home_team",
        "match_link__home_team__club",
        "match_link__away_team",
        "match_link__away_team__club",
        "match_link__season",
    ).filter(
        Q(match_link__home_team=team) | Q(match_link__away_team=team),
    )
    if season:
        queryset = queryset.filter(match_link__season=season)
    return queryset


def _team_players_queryset(
    team: Team,
    season: Season | None,
    match_data_qs: QuerySet[MatchData],
) -> QuerySet[Player]:
    teamdata_qs = TeamData.objects.filter(team=team)
    if season is not None:
        teamdata_qs = teamdata_qs.filter(season=season)

    teamdata_player_ids = TeamData.players.through.objects.filter(
        teamdata_id__in=teamdata_qs.values_list("id", flat=True),
    ).values_list("player_id", flat=True)

    match_ids = list(match_data_qs.values_list("id_uuid", flat=True))

    all_player_ids = teamdata_player_ids
    if match_ids:
        match_player_ids = MatchPlayer.objects.filter(
            team=team,
            match_data_id__in=match_ids,
        ).values_list("player_id", flat=True)
        shot_player_ids = Shot.objects.filter(
            team=team,
            match_data_id__in=match_ids,
        ).values_list("player_id", flat=True)
        all_player_ids = all_player_ids.union(match_player_ids, shot_player_ids)

    return (
        Player.objects
        .select_related("user")
        .only(
            "id_uuid",
            "profile_picture",
            "profile_picture_visibility",
            "stats_visibility",
            "goal_song_uri",
            "song_start_time",
            "goal_song_song_ids",
            "user__username",
        )
        .filter(id_uuid__in=all_player_ids)
        .order_by("user__username")
    )


def _main_roster_ids(*, team: Team, season: Season | None) -> set[str]:
    team_data_qs = TeamData.objects.filter(team=team)
    if season is not None:
        team_data_qs = team_data_qs.filter(season=season)

    return {
        str(player_id)
        for player_id in (
            team_data_qs
            .values_list("players__id_uuid", flat=True)
            .distinct()
            .exclude(players__id_uuid__isnull=True)
        )
    }


def _order_roster_players(
    *,
    roster_players: list[Player],
    main_roster_ids: set[str],
) -> list[Player]:
    main_roster_players = [
        player for player in roster_players if str(player.id_uuid) in main_roster_ids
    ]
    reserve_roster_players = [
        player
        for player in roster_players
        if str(player.id_uuid) not in main_roster_ids
    ]

    main_roster_players.sort(key=lambda p: p.user.username.lower())
    reserve_roster_players.sort(key=lambda p: p.user.username.lower())
    return [*main_roster_players, *reserve_roster_players]
