"""Object-level permissions for the player API."""

from __future__ import annotations

from rest_framework import permissions
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.player.models.player import Player


class CanModifyPlayer(permissions.BasePermission):
    """Allow reads to everyone and writes to the owner or staff."""

    message = "You do not have permission to modify this player"

    def has_object_permission(
        self,
        request: Request,
        view: APIView,
        obj: object,
    ) -> bool:
        """Authorize writes against a concrete Player object."""
        del view
        if request.method in permissions.SAFE_METHODS:
            return True
        if not isinstance(obj, Player):
            return False

        user = request.user
        if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
            return True
        return obj.user_id == getattr(user, "id", None)
