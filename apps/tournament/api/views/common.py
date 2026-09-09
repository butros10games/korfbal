"""Tournament common API endpoints and helpers."""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import APIException, NotAuthenticated, PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response

from apps.tournament.api.permissions import can_manage_tournament, is_authenticated
from apps.tournament.models import Tournament, TournamentMatch
from apps.tournament.services.editing import TournamentEditingError
from apps.tournament.services.final_groups import (
    FinalGroupError,
    resolve_tournament_qualifiers,
)


class Conflict(APIException):
    """A stale client tried to overwrite a newer result."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "This result changed on another device. Refresh and try again."


def require_authentication(request: Request) -> None:
    """Reject anonymous requests using DRF's authentication response.

    Raises:
        NotAuthenticated: If the request has no authenticated user.

    """
    if not is_authenticated(request.user):
        raise NotAuthenticated()


def require_manager(request: Request, tournament: Tournament) -> None:
    """Require an authenticated tournament manager before a management action.

    Raises:
        PermissionDenied: If the user cannot manage the tournament.

    """
    require_authentication(request)
    if not can_manage_tournament(request.user, tournament):
        raise PermissionDenied("You do not have permission to manage this tournament.")


def get_tournament(tournament_id: str) -> Tournament:
    """Load a tournament with its ownership relations for permission checks."""
    return get_object_or_404(
        Tournament.objects.select_related("owner", "organizer_club"),
        id_uuid=tournament_id,
    )


def lock_tournament_for_match(match_id: str) -> Tournament:
    """Lock the parent aggregate before any match row in a write transaction."""
    match = get_object_or_404(
        TournamentMatch.objects.only("tournament_id"),
        id_uuid=match_id,
    )
    return get_object_or_404(
        Tournament.objects.select_for_update(of=("self",)).select_related(
            "owner", "organizer_club"
        ),
        pk=match.tournament_id,
    )


def lock_tournament(tournament_id: str) -> Tournament:
    """Lock a tournament before locking any of its matches."""
    return get_object_or_404(
        Tournament.objects.select_for_update(of=("self",)).select_related(
            "owner", "organizer_club"
        ),
        id_uuid=tournament_id,
    )


def resolve_qualifiers(tournament: Tournament) -> None:
    """Refresh every planned bracket slot or abort an unsafe ranking change.

    Raises:
        Conflict: If the ranking change would alter a started bracket match.

    """
    try:
        resolve_tournament_qualifiers(tournament)
    except FinalGroupError as exc:
        raise Conflict(str(exc)) from exc


def editing_error_response(exc: TournamentEditingError) -> Response:
    """Translate a rejected planning edit to the existing conflict response."""
    return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
