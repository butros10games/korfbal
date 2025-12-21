"""Shared REST framework permission classes."""

from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView


class IsStaffOrReadOnly(BasePermission):
    """Allow read access to everyone; write access only to staff users."""

    message = "Only staff users may modify this resource."

    def has_permission(self, request: Request, view: APIView) -> bool:
        """Return True if the request should be allowed for this view."""
        if request.method in SAFE_METHODS:
            return True
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated and user.is_staff)
