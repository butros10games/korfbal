"""Permissions for club admin endpoints."""

from __future__ import annotations

from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.club.models.club import Club
from apps.player.models.player import Player


def _viewer_player(request: Request) -> Player | None:
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return Player.objects.filter(user=user).first()


class IsClubAdmin(BasePermission):
    """Allow access only to admins of the given club."""

    message = "You must be a club admin to perform this action."

    def has_permission(self, request: Request, view: APIView) -> bool:
        """Return True when the authenticated viewer is an admin of the club."""
        club_id = getattr(view, "kwargs", {}).get("id_uuid")
        if not club_id:
            # Fallback: do not grant permissions without a club context.
            return False

        player = _viewer_player(request)
        if player is None:
            return False

        return Club.objects.filter(id_uuid=club_id, admin=player).exists()
