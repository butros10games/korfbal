"""Team overview payload construction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.competition.models import (
    MatchMembership,
    RosterMembership,
    Team as SourceTeam,
)
from apps.competition.services.rosters import ROSTER_FRESHNESS
from apps.game_tracker.models import MatchData, StartingPlayerAssignment
from apps.kwt_common.utils.general_stats import build_general_stats_sync
from apps.kwt_common.utils.match_summary import build_match_summaries
from apps.kwt_common.utils.players_stats import build_player_stats_sync
from apps.player.models import Player
from apps.player.privacy import can_view_by_visibility
from apps.schedule.models import Season
from apps.schedule.queries.seasons import season_options_payload
from apps.team.models import TeamData
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

    selections = MatchMembership.objects.filter(
        team__local_team_data__team=team,
        match__local_match__season=season,
        player__in=Player.objects.all(),
    )
    match_roles = (
        _match_roles(team, match_data_qs, selections, ordered_roster_players)
        if options.include_roster
        else {}
    )
    roster: list[dict[str, Any]] = []
    if options.include_roster:
        roster = [
            {
                "id_uuid": str(player.id_uuid),
                "display_name": player.display_name,
                "username": player.display_name,
                "has_account": player.user_id is not None,
                "role_labels": sorted(match_roles.get(player.pk, set())),
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

    staff = []
    if options.include_roster:
        data = TeamData.objects.filter(team=team, season=season)
        people = (
            Player.objects
            .filter(
                Q(team_data_as_staff__in=data)
                | Q(pk__in=selections.filter(role="staff").values("player_id"))
            )
            .select_related("user")
            .distinct()
        )
        labels = {
            "COACHING_STAFF": "Technische staf",
            "MEDICAL_STAFF": "Medische staf",
            "OTHER_STAFF": "Overige staf",
        }
        roles = {}
        for observation in RosterMembership.objects.filter(
            published_team_data__in=data, ended_at=None
        ):
            roles.setdefault(observation.player_id, set()).update(
                labels[role] for role in observation.roles if role in labels
            )
        for observation in selections.filter(role="staff"):
            roles.setdefault(observation.player_id, set()).update(
                labels[role] for role in observation.roles if role in labels
            )
        staff = [
            {
                "id_uuid": str(player.pk),
                "display_name": player.display_name,
                "username": player.display_name,
                "has_account": player.user_id is not None,
                "profile_picture_url": player.get_profile_picture()
                if can_view_by_visibility(
                    visibility=player.profile_picture_visibility,
                    viewer=options.viewer_player,
                    target=player,
                )
                else player.get_placeholder_profile_picture_url(),
                "profile_url": player.get_absolute_url(),
                "role_labels": sorted(roles.get(player.pk, {"Staf"})),
            }
            for player in people
        ]

    stats_players = []
    if options.include_stats and roster_players and has_matches:
        stats_players = build_player_stats_sync(roster_players, match_data_qs)

    private_roster = (
        _private_roster_counts(team, season)
        if options.include_roster
        else {"players": 0, "staff": 0, "is_estimate": False}
    )
    return {
        "private_roster": private_roster,
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
        "staff": staff,
        "seasons": season_options_payload(seasons),
        "meta": {
            "season_id": str(season.id_uuid) if season else None,
            "season_name": season.name if season else None,
            "roster_count": len(roster) + private_roster["players"],
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
            player.display_name.lower(),
        ),
    )


def _match_roles(
    team: Team,
    match_data_qs: QuerySet[MatchData],
    selections: QuerySet[MatchMembership],
    ordered_roster_players: list[Player],
) -> dict:
    """Add selected native people and labels without altering statistics inputs."""
    match_roles = {}
    labels = {
        "starter": "Basisspeler (wedstrijd)",
        "substitute": "Wisselspeler (wedstrijd)",
        "selected": "Wedstrijdselectie (basis/wissel onbekend)",
    }
    selected_ids = set()
    for selection in selections.exclude(role="staff"):
        selected_ids.add(selection.player_id)
        match_roles.setdefault(selection.player_id, set()).add(labels[selection.role])
    present = {player.pk for player in ordered_roster_players}
    ordered_roster_players.extend(
        Player.objects
        .filter(pk__in=selected_ids - present)
        .select_related("user")
        .order_by("name", "pk")
    )
    assignments = StartingPlayerAssignment.objects.filter(
        match_data__in=match_data_qs,
        player_group__team=team,
    ).values_list("player_id", "player_group__starting_type__name")
    for player_id, group_name in assignments:
        label = (
            "Wisselspeler (wedstrijd)"
            if group_name.casefold().startswith("reserve")
            else "Basisspeler (wedstrijd)"
            if group_name.casefold() in {"aanval", "verdediging"}
            else None
        )
        if label:
            match_roles.setdefault(player_id, set()).add(label)
    return match_roles


def _private_roster_counts(team: Team, season: Season | None) -> dict:
    """Keep anonymous totals season-scoped; never sum unidentifiable variants."""
    counts = list(
        SourceTeam.objects.filter(
            local_team_data__team=team,
            local_team_data__season=season,
            roster_observed_at__gte=timezone.now() - ROSTER_FRESHNESS,
        ).values_list("private_roster_counts", flat=True)
    )
    players = max((row.get("players", 0) for row in counts), default=0)
    staff = max((row.get("staff", 0) for row in counts), default=0)
    return {
        "players": players,
        "staff": staff,
        "is_estimate": len(counts) > 1 and bool(players or staff),
    }
