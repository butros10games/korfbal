"""API endpoints for match player selection.

These endpoints replace the legacy `apps.game_tracker.urls` `/match/api/*` routes
that were previously served from Django views (and were coupled to templates).

The React SPA should use these endpoints via `/api/match/...`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.request import Request
from rest_framework.response import Response

from apps.game_tracker.composition import apply_player_designation
from apps.game_tracker.models import MatchData, MatchPlayer, PlayerGroup
from apps.game_tracker.services.match_mutations import MatchRevisionConflictError
from apps.game_tracker.services.player_designation import (
    PLAYER_GROUP_EDIT_PERMISSION_ERROR,
    DesignatePlayersCommand,
    PlayerDesignationPermissionError,
    PlayerDesignationSelection,
    PlayerDesignationValidationError,
    can_edit_player_groups,
)
from apps.game_tracker.services.player_groups import club_lineup_players
from apps.game_tracker.services.player_search import player_name_match_score
from apps.game_tracker.services.tracker_access import SESSION_KEY, has_tracker_grant
from apps.player.models import Player
from apps.player.privacy import can_view_by_visibility
from apps.player.services.player_queries import player_access_queryset
from apps.schedule.models import Match
from apps.team.models import Team, TeamData


# DRF's ``api_view`` returns a callable view object that is valid input for
# drf-spectacular but narrower than its type annotation permits.
_function_schema = cast(Any, extend_schema)


class HasPlayerGroupSession(BasePermission):
    """Require a login or invitation session before resolving the lineup scope."""

    def has_permission(self, request: Request, view: object) -> bool:
        """Scope and expiry are checked against the resolved match/team below."""
        return bool(request.user.is_authenticated or request.session.get(SESSION_KEY))


def _viewer_player(request: Request) -> Player | None:
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return player_access_queryset().filter(user=user).first()


def _profile_picture_for(viewer: Player | None, target: Player) -> str:
    if can_view_by_visibility(
        visibility=target.profile_picture_visibility,
        viewer=viewer,
        target=target,
    ):
        return target.get_profile_picture()
    return target.get_placeholder_profile_picture_url()


def _player_group_editor_error(
    *,
    request: Request,
    match: Match,
    team: Team,
) -> Response | None:
    if can_edit_player_groups(user=request.user, match=match, team=team):
        return None
    if request.auth is None and has_tracker_grant(
        request.session.get(SESSION_KEY, {}), match=match, team=team
    ):
        return None
    return Response({"error": PLAYER_GROUP_EDIT_PERMISSION_ERROR}, status=403)


@_function_schema(responses=OpenApiTypes.OBJECT)
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
                queryset=player_access_queryset(),
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
                "user": {"username": player.display_name},
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

    captain_id = (
        MatchPlayer.objects
        .filter(
            match_data=match_data,
            team_id=team_id,
            is_captain=True,
            player_id__in=[
                player["id_uuid"]
                for group in player_groups_data
                for player in group["players"]
            ],
        )
        .values_list("player_id", flat=True)
        .first()
    )
    return Response({
        "captain_player_id": str(captain_id) if captain_id else None,
        "player_groups": player_groups_data,
        "live_revision": match_data.live_revision,
    })


@_function_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([HasPlayerGroupSession])
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
    team_data = TeamData.objects.filter(
        team=team_model, season_id=match_model.season_id
    ).first()

    excluded_ids = (
        PlayerGroup.objects
        .filter(match_data=match_data, team=team_model, players__id_uuid__isnull=False)
        .values_list("players__id_uuid", flat=True)
        .distinct()
    )
    players = (
        player_access_queryset()
        .filter(team_data_as_player=team_data)
        .exclude(id_uuid__in=excluded_ids)
        if team_data is not None
        else Player.objects.none()
    )

    viewer = _viewer_player(request)

    return Response(
        {
            "players": [
                {
                    "id_uuid": str(player.id_uuid),
                    "user": {"username": player.display_name},
                    "get_profile_picture": _profile_picture_for(viewer, player),
                }
                for player in players
            ],
        },
    )


MIN_PLAYER_NAME_LENGTH = 3
MAX_PLAYER_NAME_LENGTH = 50


@_function_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([HasPlayerGroupSession])
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

    # Use the same club/date/season boundary for discovery and guest writes.
    roster_players = (
        player_access_queryset()
        .filter(
            pk__in=club_lineup_players(match=match_model, team=team_model).values("pk")
        )
        .exclude(id_uuid__in=excluded_ids)
    )
    ranked_players = [
        (score, player)
        for player in roster_players
        if (
            score := player_name_match_score(
                search_query,
                username=player.display_name,
                first_name=player.user.first_name if player.user_id else "",
                last_name=player.user.last_name if player.user_id else "",
            )
        )
        is not None
    ]
    players = [
        player
        for _, player in sorted(
            ranked_players,
            key=lambda item: (item[0], item[1].display_name.casefold()),
        )
    ]

    viewer = _viewer_player(request)

    return Response(
        {
            "players": [
                {
                    "id_uuid": str(player.id_uuid),
                    "user": {"username": player.display_name},
                    "get_profile_picture": _profile_picture_for(viewer, player),
                }
                for player in players
            ],
        },
    )


def _parse_designation_payload(request: Request) -> dict[str, Any] | None:
    """Return DRF's parsed payload when it is object-shaped."""
    if not isinstance(request.data, Mapping):
        return None
    if not isinstance(request.data.get("make_captain", False), bool):
        return None
    return dict(request.data)


def _extract_designation_players(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_players = data.get("players")
    if not isinstance(raw_players, list):
        return []
    return [entry for entry in raw_players if isinstance(entry, dict)]


def _prepare_player_designation(
    request: Request,
) -> tuple[DesignatePlayersCommand | None, Response | None]:
    """Validate HTTP payload shape and build a typed application command."""
    data = _parse_designation_payload(request)
    if data is None:
        return None, Response({"error": "Invalid JSON data"}, status=400)

    selected_players = _extract_designation_players(data)
    if not selected_players:
        return None, Response({"error": "No player selected"}, status=400)

    selections: list[PlayerDesignationSelection] = []
    for player_data in selected_players:
        player_id = player_data.get("id_uuid")
        if not isinstance(player_id, str) or not player_id:
            return None, Response({"error": "Invalid player"}, status=400)
        source_group_id = player_data.get("groupId")
        selections.append(
            PlayerDesignationSelection(
                player_id=player_id,
                source_group_id=(
                    source_group_id
                    if isinstance(source_group_id, str) and source_group_id
                    else None
                ),
            )
        )

    make_captain = data.get("make_captain", False)

    new_group_id = data.get("new_group_id")
    if new_group_id and not isinstance(new_group_id, str):
        return None, Response({"error": "Unknown player group"}, status=400)

    expected_revision = data.get("expected_revision")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        return None, Response(
            {"expected_revision": ["A non-negative integer is required."]},
            status=400,
        )

    return (
        DesignatePlayersCommand(
            players=tuple(selections),
            target_group_id=(
                new_group_id if isinstance(new_group_id, str) and new_group_id else None
            ),
            expected_revision=expected_revision,
            make_captain=make_captain,
        ),
        None,
    )


@_function_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
@api_view(["POST"])
@permission_classes([HasPlayerGroupSession])
def player_designation(request: Request) -> Response:
    """Designate players to/from a player group.

    Expected payload (legacy-compatible):
        {
          "players": [{"id_uuid": "...", "groupId": "..."}, ...],
          "new_group_id": "..." | null
        }

    """
    # SessionAuthentication already checks signed-in browser users; bearer
    # clients do not need CSRF. Anonymous invitation sessions need it explicitly.
    if not request.user.is_authenticated:
        SessionAuthentication().enforce_csrf(request)
    command, error_response = _prepare_player_designation(request)
    if error_response is not None:
        return error_response
    assert command is not None
    try:
        result = apply_player_designation(
            actor=request.user,
            command=command,
            # Bearer requests use the token's own permissions. Do not borrow
            # cookie-based capabilities without session CSRF authentication.
            tracker_grants=(
                request.session.get(SESSION_KEY, {}) if request.auth is None else {}
            ),
        )
    except MatchRevisionConflictError as exc:
        return Response(
            {
                "code": "revision_conflict",
                "detail": str(exc),
                "expected_revision": exc.expected_revision,
                "live_revision": exc.live_revision,
            },
            status=409,
        )
    except PlayerDesignationPermissionError as exc:
        return Response({"error": str(exc)}, status=403)
    except PlayerDesignationValidationError as exc:
        return Response({"error": str(exc)}, status=400)

    return Response({"success": True, "live_revision": result.revision})
