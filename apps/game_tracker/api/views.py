"""API endpoints for match player selection.

These endpoints replace the legacy `apps.game_tracker.urls` `/match/api/*` routes
that were previously served from Django views (and were coupled to templates).

The React SPA should use these endpoints via `/api/match/...`.
"""

from __future__ import annotations

import json
from typing import Any

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.game_tracker.models import GroupType, MatchData, PlayerGroup
from apps.player.models import Player
from apps.player.privacy import can_view_by_visibility
from apps.schedule.models import Match
from apps.team.models import Team, TeamData


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

    team_data = TeamData.objects.get(team=team_model, season=match_model.season)

    players = team_data.players.all()
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

    players = Player.objects.filter(user__username__icontains=search_query).exclude(
        id_uuid__in=[
            player.id_uuid
            for player_group in player_groups
            for player in player_group.players.all()
        ],
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

    raw_players = data.get("players", [])
    selected_players = raw_players if isinstance(raw_players, list) else []
    new_group_id = data.get("new_group_id")

    if not selected_players:
        return Response({"error": "No player selected"}, status=400)

    normalized_players: list[dict[str, Any]] = [
        entry for entry in selected_players if isinstance(entry, dict)
    ]

    player_group_model: PlayerGroup | None = None
    if new_group_id:
        player_group_model = PlayerGroup.objects.get(id_uuid=new_group_id)

        if not _validate_target_group_capacity(
            player_group_model=player_group_model,
            selected_players=normalized_players,
        ):
            return Response({"error": "Too many players selected"}, status=400)

    for player_data in normalized_players:
        player_id = player_data.get("id_uuid")
        if not player_id:
            continue

        old_group_id = player_data.get("groupId")
        if old_group_id:
            PlayerGroup.objects.get(id_uuid=old_group_id).players.remove(
                Player.objects.get(id_uuid=player_id),
            )

        if player_group_model:
            player_group_model.players.add(Player.objects.get(id_uuid=player_id))

    return Response({"success": True})
