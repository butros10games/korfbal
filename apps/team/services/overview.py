"""Team overview payload construction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from apps.kwt_common.utils.general_stats import build_general_stats_sync
from apps.kwt_common.utils.match_summary import build_match_summaries
from apps.kwt_common.utils.players_stats import build_player_stats_sync
from apps.player.models import Player
from apps.player.privacy import can_view_by_visibility
from apps.schedule.models import Season
from apps.schedule.queries.seasons import season_options_payload
from apps.team.models.team import Team
from apps.team.queries.overview import (
    main_roster_ids,
    team_matches,
    team_players,
)


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
    match_data_qs = team_matches(team, season)
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

    has_matches = match_data_qs.exists() if options.include_stats else False
    stats_general = None
    if options.include_stats and has_matches:
        stats_general = build_general_stats_sync(match_data_qs)

    roster_players: list[Player] = []
    if options.include_roster or options.include_stats:
        roster_players = list(team_players(team, season, match_data_qs))

    roster_ids = main_roster_ids(team=team, season=season) if roster_players else set()
    ordered_roster_players = _order_roster_players(
        roster_players=roster_players,
        main_roster_ids=roster_ids,
    )

    roster: list[dict[str, str]] = []
    if options.include_roster:
        roster = [
            {
                "id_uuid": str(player.id_uuid),
                "display_name": player.user.username,
                "username": player.user.username,
                "roster_role": (
                    "main" if str(player.id_uuid) in roster_ids else "reserve"
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
    if options.include_stats and roster_players and has_matches:
        stats_players = build_player_stats_sync(roster_players, match_data_qs)

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
        "seasons": season_options_payload(seasons),
        "meta": {
            "season_id": str(season.id_uuid) if season else None,
            "season_name": season.name if season else None,
            "roster_count": len(roster),
            "viewer_can_manage_goal_songs": options.viewer_can_manage_goal_songs,
            "fallback_goal_song_audio_urls": options.fallback_goal_song_audio_urls,
        },
    }


def _order_roster_players(
    *,
    roster_players: list[Player],
    main_roster_ids: set[str],
) -> list[Player]:
    return sorted(
        roster_players,
        key=lambda player: (
            str(player.id_uuid) not in main_roster_ids,
            player.user.username.lower(),
        ),
    )
