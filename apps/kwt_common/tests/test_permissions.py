"""Permission contract tests for shared Korfbal API views."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from rest_framework.request import Request
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from apps.kwt_common.api.permissions import IsStaffOrReadOnly


def _request(method: str, *, user: object | None = None) -> Request:
    raw_request = APIRequestFactory().generic(method, "/resource/")
    request = Request(raw_request)
    if user is not None:
        request._user = user
    return request


def test_staff_permission_allows_anonymous_safe_methods() -> None:
    """Public resources remain readable without authentication."""
    permission = IsStaffOrReadOnly()

    for method in ("GET", "HEAD", "OPTIONS"):
        assert permission.has_permission(_request(method), APIView()) is True


def test_staff_permission_rejects_unauthenticated_and_non_staff_writes() -> None:
    """Neither a missing user nor a normal account may mutate shared resources."""
    permission = IsStaffOrReadOnly()

    assert permission.has_permission(_request("POST"), APIView()) is False
    assert (
        permission.has_permission(
            _request(
                "PATCH",
                user=SimpleNamespace(is_authenticated=True, is_staff=False),
            ),
            APIView(),
        )
        is False
    )


def test_staff_permission_allows_authenticated_staff_writes() -> None:
    """Staff accounts may use every unsafe method."""
    permission = IsStaffOrReadOnly()
    staff = cast(
        object,
        SimpleNamespace(is_authenticated=True, is_staff=True),
    )

    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert (
            permission.has_permission(_request(method, user=staff), APIView()) is True
        )
