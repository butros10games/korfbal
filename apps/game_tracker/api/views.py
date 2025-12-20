"""API endpoints for match player selection.

These endpoints replace the legacy `apps.game_tracker.urls` `/match/api/*` routes
that were previously served from Django views (and were coupled to templates).

The React SPA should use these endpoints via `/api/match/...`.
"""

from __future__ import annotations

import json
from typing import Any

from django.db.models import Q, QuerySet
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.game_tracker.models import GroupType, MatchData, MatchPlayer, PlayerGroup
from apps.player.models import Player
from apps.player.privacy import can_view_by_visibility
from apps.schedule.models import Match
from apps.team.models import Team, TeamData


def _sync_match_players_for_team(*, match_data: MatchData, team: Team) -> None:
    """Sync MatchPlayer rows from current PlayerGroup assignments.

    Why:
        - Match stats prefers MatchPlayer (explicit roster) over heuristics.
        - PlayerGroup assignments are per-match and therefore preserve history.
    """
    desired_ids = set(
        Player.objects
        .filter(
            player_groups__match_data=match_data,
            player_groups__team=team,
        )
        .values_list("id_uuid", flat=True)
        .distinct()
    )

    existing_ids = set(
        MatchPlayer.objects
        .filter(match_data=match_data, team=team)
        .values_list("player_id", flat=True)
        .distinct()
    )

    to_create = desired_ids - existing_ids
    to_delete = existing_ids - desired_ids

    if to_create:
        MatchPlayer.objects.bulk_create(
            [
                MatchPlayer(match_data=match_data, team=team, player_id=player_id)
                for player_id in to_create
            ],
            ignore_conflicts=True,
        )

    if to_delete:
        MatchPlayer.objects.filter(
            match_data=match_data,
            team=team,
            player_id__in=list(to_delete),
        ).delete()


def _sync_match_players(*, match_data: MatchData) -> None:
    match = match_data.match_link
    _sync_match_players_for_team(match_data=match_data, team=match.home_team)
    _sync_match_players_for_team(match_data=match_data, team=match.away_team)


def _viewer_player(request: Request) -> Player | None:
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return Player.objects.filter(user=user).first()


def _profile_picture_for(viewer: Player | None, target: Player) -> str:
    if can_view_by_visibility(
        visibility=target.profile_picture_visibility,
        viewer=viewer,
        target=target,
    ):
        return target.get_profile_picture()
    return target.get_placeholder_profile_picture_url()


def _get_player_groups(match_id: str, team_id: str) -> QuerySet[PlayerGroup]:
    match_model = get_object_or_404(Match, id_uuid=match_id)
    team_model = get_object_or_404(Team, id_uuid=team_id)
    match_data = MatchData.objects.get(match_link=match_model)

    return PlayerGroup.objects.filter(match_data=match_data, team=team_model).order_by(
        "starting_type__order",
    )


def _ensure_player_groups_exist(match_model: Match, match_data: MatchData) -> None:
    """Create missing PlayerGroup rows for the match's teams.

    The legacy server-rendered match tracker view implicitly created the
    PlayerGroup rows (Aanval/Verdediging/Reserve, etc.) the first time it was
    opened. The SPA expects the same behavior from the REST API.

    This is intentionally idempotent.
    """
    group_types = list(GroupType.objects.all().order_by("order", "name"))
    if not group_types:
        return

    teams = [match_model.home_team, match_model.away_team]
    for team in teams:
        for group_type in group_types:
            PlayerGroup.objects.get_or_create(
                match_data=match_data,
                team=team,
                starting_type=group_type,
                defaults={"current_type": group_type},
            )


@api_view(["GET"])
@permission_classes([AllowAny])
def player_overview_data(request: Request, match_id: str, team_id: str) -> Response:
    """Return player groups for a match/team.

    Response shape matches the legacy endpoint:
        {"player_groups": [
            {"id_uuid", "starting_type": {"name"}, "players": [...]},
            ...,
        ]}

    """
    match_model = get_object_or_404(Match, id_uuid=match_id)
    match_data = MatchData.objects.get(match_link=match_model)

    if request.user.is_authenticated:
        _ensure_player_groups_exist(match_model, match_data)

    player_groups = PlayerGroup.objects.filter(
        match_data=match_data,
        team_id=team_id,
    ).order_by("starting_type__order")
    viewer = _viewer_player(request)

    player_groups_data: list[dict[str, Any]] = []
    for player_group in player_groups:
        players_data = [
            {
                "id_uuid": str(player.id_uuid),
                "user": {"username": player.user.username},
                "get_profile_picture": _profile_picture_for(viewer, player),
            }
            for player in player_group.players.all()
        ]
        player_groups_data.append(
            {
                "id_uuid": str(player_group.id_uuid),
                "starting_type": {"name": player_group.starting_type.name},
                "players": players_data,
            },
        )

    return Response({"player_groups": player_groups_data})


@api_view(["GET"])
@permission_classes([AllowAny])
def players_team(request: Request, match_id: str, team_id: str) -> Response:
    """Return team players that are not in a player group for this match."""
    match_model = get_object_or_404(Match, id_uuid=match_id)
    team_model = get_object_or_404(Team, id_uuid=team_id)

    match_data = MatchData.objects.get(match_link=match_model)
    if request.user.is_authenticated:
        _ensure_player_groups_exist(match_model, match_data)

    team_data = (
        TeamData.objects
        .filter(team=team_model, season=match_model.season)
        .prefetch_related("players")
        .first()
    )

    players = (
        team_data.players.all() if team_data is not None else Player.objects.none()
    )
    player_groups = PlayerGroup.objects.filter(match_data=match_data, team=team_model)

    for player_group in player_groups:
        players = players.exclude(id_uuid__in=player_group.players.all())

    viewer = _viewer_player(request)

    return Response(
        {
            "players": [
                {
                    "id_uuid": str(player.id_uuid),
                    "user": {"username": player.user.username},
                    "get_profile_picture": _profile_picture_for(viewer, player),
                }
                for player in players
            ],
        },
    )


MIN_PLAYER_NAME_LENGTH = 3
MAX_PLAYER_NAME_LENGTH = 50


@api_view(["GET"])
@permission_classes([AllowAny])
def player_search(request: Request, match_id: str, team_id: str) -> Response:
    """Search for players by username, excluding already-grouped players."""
    search_query = (request.query_params.get("search") or "").strip()
    if not search_query:
        return Response({"error": "No player selected"}, status=400)

    if len(search_query) < MIN_PLAYER_NAME_LENGTH:
        return Response(
            {
                "success": False,
                "error": "Player name should be at least 3 characters long",
            },
        )

    if len(search_query) > MAX_PLAYER_NAME_LENGTH:
        return Response(
            {
                "success": False,
                "error": "Player name should be at most 50 characters long",
            },
        )

    match_model = get_object_or_404(Match, id_uuid=match_id)
    team_model = get_object_or_404(Team, id_uuid=team_id)

    match_data = MatchData.objects.get(match_link=match_model)
    if request.user.is_authenticated:
        _ensure_player_groups_exist(match_model, match_data)

    player_groups = PlayerGroup.objects.filter(match_data=match_data, team=team_model)
    # Important: `values_list('players__id_uuid')` can yield NULL rows for empty
    # groups (left join). Using that in a `NOT IN (NULL)` exclusion would filter
    # out *all* players. Filter out NULLs up-front.
    excluded_ids = (
        player_groups
        .filter(players__id_uuid__isnull=False)
        .values_list("players__id_uuid", flat=True)
        .distinct()
    )

    match_date = match_model.start_time.date()

    # Only show players who belong to this club context.
    # - TeamData is season-scoped (legacy) and historically incomplete.
    #   We therefore consider *any* team of the club in the match season.
    # - club membership is date-scoped (new) and preferred when available.
    club_roster_filter = Q(
        team_data_as_player__team__club=team_model.club,
        team_data_as_player__season=match_model.season,
    ) | Q(
        team_data_as_coach__team__club=team_model.club,
        team_data_as_coach__season=match_model.season,
    )

    membership_filter = Q(
        club_membership_links__club=team_model.club,
        club_membership_links__start_date__lte=match_date,
    ) & (
        Q(club_membership_links__end_date__isnull=True)
        | Q(club_membership_links__end_date__gte=match_date)
    )

    allowed_filter = club_roster_filter | membership_filter

    search_filter = (
        Q(user__username__icontains=search_query)
        | Q(user__first_name__icontains=search_query)
        | Q(user__last_name__icontains=search_query)
        | Q(user__email__icontains=search_query)
    )

    players = (
        Player.objects
        .filter(search_filter)
        .filter(allowed_filter)
        .exclude(id_uuid__in=excluded_ids)
        .distinct()
    )

    viewer = _viewer_player(request)

    return Response(
        {
            "players": [
                {
                    "id_uuid": str(player.id_uuid),
                    "user": {"username": player.user.username},
                    "get_profile_picture": _profile_picture_for(viewer, player),
                }
                for player in players
            ],
        },
    )


MAX_RESERVE_PLAYERS = 16
MAX_STARTING_PLAYERS = 4


def _parse_designation_payload(request: Request) -> dict[str, Any] | None:
    """Parse and normalize the player designation payload (best-effort)."""
    if isinstance(request.data, dict):
        return request.data

    try:
        parsed = json.loads(request.body)
    except Exception:
        return None

    return parsed if isinstance(parsed, dict) else None


def _extract_designation_players(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_players = data.get("players")
    if not isinstance(raw_players, list):
        return []
    return [entry for entry in raw_players if isinstance(entry, dict)]


def _get_player_group(group_id: object) -> PlayerGroup | None:
    if not isinstance(group_id, str) or not group_id:
        return None
    return PlayerGroup.objects.filter(id_uuid=group_id).first()


def _apply_designation(
    *,
    selected_players: list[dict[str, Any]],
    target_group: PlayerGroup | None,
) -> PlayerGroup | None:
    """Apply the designation changes and return a group usable for syncing."""
    resolved_group = target_group

    for player_data in selected_players:
        player_id = player_data.get("id_uuid")
        if not player_id:
            continue

        old_group = _get_player_group(player_data.get("groupId"))
        if old_group is not None:
            old_group.players.remove(Player.objects.get(id_uuid=player_id))
            if resolved_group is None:
                resolved_group = old_group

        if target_group is not None:
            target_group.players.add(Player.objects.get(id_uuid=player_id))

    return resolved_group


def _validate_target_group_capacity(
    *,
    player_group_model: PlayerGroup,
    selected_players: list[dict[str, Any]],
) -> bool:
    """Return True if adding selected players would not overflow group limits."""
    is_reserve_group = player_group_model.starting_type.name == "Reserve"
    max_group_players = (
        MAX_RESERVE_PLAYERS if is_reserve_group else MAX_STARTING_PLAYERS
    )

    selected_ids = {
        player_data.get("id_uuid")
        for player_data in selected_players
        if player_data.get("id_uuid")
    }

    already_in_target_group = set(
        player_group_model.players.filter(id_uuid__in=selected_ids).values_list(
            "id_uuid",
            flat=True,
        ),
    )

    players_to_add_count = len(selected_ids - already_in_target_group)
    final_group_size = player_group_model.players.count() + players_to_add_count
    return final_group_size <= max_group_players


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def player_designation(request: Request) -> Response:
    """Designate players to/from a player group.

    Expected payload (legacy-compatible):
        {
          "players": [{"id_uuid": "...", "groupId": "..."}, ...],
          "new_group_id": "..." | null
        }

    """
    data = _parse_designation_payload(request)
    if data is None:
        return Response({"error": "Invalid JSON data"}, status=400)

    selected_players = _extract_designation_players(data)
    if not selected_players:
        return Response({"error": "No player selected"}, status=400)

    new_group_id = data.get("new_group_id")
    target_group = _get_player_group(new_group_id)

    if new_group_id and target_group is None:
        return Response({"error": "Unknown player group"}, status=400)

    if target_group is not None and not _validate_target_group_capacity(
        player_group_model=target_group,
        selected_players=selected_players,
    ):
        return Response({"error": "Too many players selected"}, status=400)

    resolved_group = _apply_designation(
        selected_players=selected_players,
        target_group=target_group,
    )

    # Keep MatchPlayer roster in sync with PlayerGroup assignments.
    # This ensures match stats can reliably place players on the correct side.
    if resolved_group is not None:
        _sync_match_players(match_data=resolved_group.match_data)

    return Response({"success": True})
