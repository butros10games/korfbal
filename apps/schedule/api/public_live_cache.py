"""Anonymous cache hits inside Django's security and response middleware."""

from collections.abc import Callable
import re
from uuid import UUID

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils.cache import patch_vary_headers
from rest_framework.renderers import JSONRenderer

from apps.game_tracker.composition import read_cached_live, read_public_match

from .match_viewset_live import _parse_since_revision


_ROUTE = re.compile(r"/api/matches/(?P<id>[0-9a-fA-F-]{36})/live/(?P<poll>poll/)?")


_MATCH_ROUTE = re.compile(
    r"/api/matches/(?P<id>[0-9a-fA-F-]{36})/(?P<resource>summary|stats|events|shots)/"
)


class PublicLiveCacheMiddleware:
    """Shortcut only anonymous JSON cache hits; retain all other route semantics."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        """Keep the full route available for recovery and authenticated requests."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Serve the exact public contract without async/sync view dispatch hops."""
        route = _ROUTE.fullmatch(request.path_info)
        if route is None:
            return self._public_match(request)
        if (
            request.method != "GET"
            or request.headers.get("Authorization") is not None
            or settings.SESSION_COOKIE_NAME in request.COOKIES
            or request.headers.get("Accept", "*/*") not in {"*/*", "application/json"}
            or set(request.GET) - {"since_revision", "timeout"}
        ):
            return self.get_response(request)
        try:
            match_id = str(UUID(route["id"]))
        except ValueError:
            return self.get_response(request)
        since_revision = None
        if route["poll"]:
            since_revision = _parse_since_revision(request.GET.get("since_revision"))
            if since_revision is None:
                return self.get_response(request)
        request.get_host()
        payload = read_cached_live(match_id=match_id, since_revision=since_revision)
        if payload is None:
            response = self.get_response(request)
            response["X-Korfbal-Live-Cache"] = "miss"
            return response
        response = HttpResponse(
            JSONRenderer().render(payload), content_type="application/json"
        )
        response["X-Korfbal-Live-Cache"] = "hit"
        response["Allow"] = "GET, HEAD, OPTIONS"
        patch_vary_headers(response, ("Accept", "Cookie"))
        return response

    def _public_match(self, request: HttpRequest) -> HttpResponse:
        route = _MATCH_ROUTE.fullmatch(request.path_info)
        if route is None:
            return self.get_response(request)
        resource = route["resource"]
        allowed = (
            {"since_revision", "identity_version"}
            if resource in {"events", "shots"}
            else set()
        )
        if (
            request.method != "GET"
            or request.headers.get("Authorization") is not None
            or settings.SESSION_COOKIE_NAME in request.COOKIES
            or request.headers.get("Accept", "*/*") not in {"*/*", "application/json"}
            or set(request.GET) - allowed
        ):
            return self.get_response(request)
        try:
            match_id = str(UUID(route["id"]))
            raw_revision = request.GET.get("since_revision")
            revision = int(raw_revision) if raw_revision is not None else None
            if revision is not None and revision < 0:
                return self.get_response(request)
        except ValueError:
            return self.get_response(request)
        request.get_host()
        payload = read_public_match(
            match_id=match_id,
            resource=resource,
            since_revision=revision,
            identity_version=request.GET.get("identity_version"),
        )
        if payload is None:
            return self.get_response(request)
        response = HttpResponse(
            JSONRenderer().render(payload), content_type="application/json"
        )
        response["X-Korfbal-Public-Read"] = "shared"
        response["Allow"] = "GET, HEAD, OPTIONS"
        patch_vary_headers(response, ("Accept", "Cookie"))
        return response
