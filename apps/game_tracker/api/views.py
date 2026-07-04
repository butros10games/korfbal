"""API endpoints for match player selection.

These endpoints replace the legacy `apps.game_tracker.urls` `/match/api/*` routes
that were previously served from Django views (and were coupled to templates).

The React SPA should use these endpoints via `/api/match/...`.
"""

from __future__ import annotations

import json
from typing import Any

from django.db import transaction
from django.db.models import Prefetch, Q, QuerySet
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.game_tracker.models import MatchData, PlayerGroup
from apps.game_tracker.services.player_designation import (
    apply_designation,
    can_edit_player_groups,
    get_player_group,
    resolve_designation_context,
    sync_match_players,
    validate_target_group_capacity,
)
from apps.player.models import Player
from apps.player.privacy import can_view_by_visibility
from apps.schedule.models import Match
from apps.team.models import Team, TeamData


PLAYER_GROUP_EDIT_PERMISSION_ERROR = "You do not have permission to edit player groups."


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


def _player_group_editor_error(
    *,
    request: Request,
    match: Match,
    team: Team,
) -> Response | None:
    if can_edit_player_groups(user=request.user, match=match, team=team):
        return None
    return Response({"error": PLAYER_GROUP_EDIT_PERMISSION_ERROR}, status=403)


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

    player_groups = (
        PlayerGroup.objects
        .filter(
            match_data=match_data,
            team_id=team_id,
        )
        .select_related(
            "starting_type",
        )
        .prefetch_related(
            Prefetch(
                "players",
                queryset=Player.objects.select_related("user"),
            ),
        )
        .order_by("starting_type__order")
    )
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
@permission_classes([IsAuthenticated])
def players_team(request: Request, match_id: str, team_id: str) -> Response:
    """Return team players that are not in a player group for this match."""
    match_model = get_object_or_404(Match, id_uuid=match_id)
    team_model = get_object_or_404(Team, id_uuid=team_id)
    permission_error = _player_group_editor_error(
        request=request,
        match=match_model,
        team=team_model,
    )
    if permission_error is not None:
        return permission_error

    match_data = MatchData.objects.get(match_link=match_model)
    team_data = (
        TeamData.objects
        .filter(team=team_model, season=match_model.season)
        .prefetch_related("players")
        .first()
    )

    excluded_ids = (
        PlayerGroup.objects
        .filter(match_data=match_data, team=team_model, players__id_uuid__isnull=False)
        .values_list("players__id_uuid", flat=True)
        .distinct()
    )
    players = (
        team_data.players.exclude(id_uuid__in=excluded_ids).select_related("user")
        if team_data is not None
        else Player.objects.none()
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


MIN_PLAYER_NAME_LENGTH = 3
MAX_PLAYER_NAME_LENGTH = 50


@api_view(["GET"])
@permission_classes([IsAuthenticated])
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
    permission_error = _player_group_editor_error(
        request=request,
        match=match_model,
        team=team_model,
    )
    if permission_error is not None:
        return permission_error

    match_data = MatchData.objects.get(match_link=match_model)
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

    match_date = timezone.localdate(match_model.start_time)

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
    )

    players = (
        Player.objects
        .filter(search_filter)
        .filter(allowed_filter)
        .exclude(id_uuid__in=excluded_ids)
        .distinct()
        .select_related("user")
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


def _parse_designation_payload(request: Request) -> dict[str, Any] | None:
    """Parse and normalize the player designation payload (best-effort)."""
    if isinstance(request.data, dict):
        return request.data

    try:
        parsed = json.loads(request.body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

    return parsed if isinstance(parsed, dict) else None


def _extract_designation_players(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_players = data.get("players")
    if not isinstance(raw_players, list):
        return []
    return [entry for entry in raw_players if isinstance(entry, dict)]


def _prepare_player_designation(
    request: Request,
) -> tuple[list[dict[str, Any]], PlayerGroup | None, Response | None]:
    """Validate payload and return selected players and target group."""
    data = _parse_designation_payload(request)
    if data is None:
        return [], None, Response({"error": "Invalid JSON data"}, status=400)

    selected_players = _extract_designation_players(data)
    if not selected_players:
        return [], None, Response({"error": "No player selected"}, status=400)

    new_group_id = data.get("new_group_id")
    target_group = get_player_group(new_group_id)
    if new_group_id and target_group is None:
        return [], None, Response({"error": "Unknown player group"}, status=400)

    if target_group is not None and not validate_target_group_capacity(
        player_group_model=target_group,
        selected_players=selected_players,
    ):
        return [], None, Response({"error": "Too many players selected"}, status=400)

    return selected_players, target_group, None


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
    selected_players, target_group, error_response = _prepare_player_designation(
        request
    )
    if error_response is not None:
        return error_response

    match_context, team_context = resolve_designation_context(
        selected_players=selected_players,
        target_group=target_group,
    )
    if match_context is None or team_context is None:
        return Response({"error": "Invalid player group context"}, status=400)

    if not can_edit_player_groups(
        user=request.user,
        match=match_context,
        team=team_context,
    ):
        return Response({"error": PLAYER_GROUP_EDIT_PERMISSION_ERROR}, status=403)

    with transaction.atomic():
        resolved_group, assignment_error = apply_designation(
            selected_players=selected_players,
            target_group=target_group,
        )
        if assignment_error is not None:
            transaction.set_rollback(True)
            return Response({"error": assignment_error}, status=400)

        # Keep MatchPlayer roster in sync with PlayerGroup assignments.
        # This ensures match stats can reliably place players on the correct side.
        if resolved_group is not None:
            sync_match_players(match_data=resolved_group.match_data)

    return Response({"success": True})
