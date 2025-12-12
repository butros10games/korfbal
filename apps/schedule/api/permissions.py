"""Custom API permissions for korfbal schedule endpoints."""

from __future__ import annotations

from typing import Any, cast

from rest_framework.permissions import BasePermission
from rest_framework.request import Request


class IsCoachOrAdmin(BasePermission):
    """Allow access to authenticated coaches and admins.

    Rules:
    - Admins: Django staff users.
    - Coaches: users in the "coach" group (case-insensitive).

    Note: this is intentionally simple. If you later model coach/team roles,
    replace the group check with that domain logic.
    """

    message = "You do not have permission to edit match events."

    def has_permission(self, request: Request, view: object) -> bool:
        """Return True if the request user is an admin or coach."""
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
            return True

        # Group name-based permission for coaches.
        try:
            groups = getattr(user, "groups", None)
            if groups is None:
                return False
            return bool(cast(Any, groups).filter(name__iexact="coach").exists())
        except Exception:
            return False
