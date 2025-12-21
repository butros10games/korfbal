"""Operational API endpoints for the Korfbal project."""

from __future__ import annotations

from django.core.cache import cache
from rest_framework import permissions
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.kwt_common.utils.slow_requests import slow_request_buffer_ttl_s


_CACHE_KEY = "korfbal:slow_requests"


class SlowRequestsAPIView(APIView):
    """Get/clear a rolling buffer of slow requests.

    This is intended to make it easy to see *which* endpoints are slow without
    scanning application logs.

    GET:
      - ?limit=50 (default)

    DELETE:
      - clears the buffer

    """

    permission_classes = (permissions.IsAdminUser,)

    def get(self, request: Request) -> Response:
        """Return the newest entries in the slow-request buffer."""
        raw_limit = request.query_params.get("limit")
        limit = 50
        if raw_limit:
            try:
                limit = int(raw_limit)
            except ValueError:
                limit = 50
        limit = max(1, min(limit, 500))

        items = cache.get(_CACHE_KEY) or []
        if not isinstance(items, list):
            items = []

        return Response({"count": len(items), "items": items[:limit]})

    def delete(self, request: Request) -> Response:
        """Clear the slow-request buffer."""
        cache.set(_CACHE_KEY, [], timeout=slow_request_buffer_ttl_s())
        return Response({"ok": True})
