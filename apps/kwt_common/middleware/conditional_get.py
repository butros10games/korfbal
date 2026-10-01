"""Answer unchanged API reads with an empty ``304 Not Modified``.

The web and native clients refetch on focus, reconnect and polling intervals, and
most of those reads return the bytes the client already holds. An entity tag lets
the client's HTTP cache send ``If-None-Match``; the view still runs, so permission
and data changes are always reflected, but an identical body is not sent again.

``Cache-Control: private, no-cache`` makes the stored copy usable only after this
revalidation, on every platform, and keeps it out of shared caches. Responses
that already declare a cache policy (``no-store`` media and credential reads,
immutable logos) keep it.
"""

from __future__ import annotations

from http import HTTPStatus

from django.http import HttpRequest, HttpResponseBase
from django.middleware.http import ConditionalGetMiddleware


API_PREFIX = "/api/"
JSON_CONTENT_TYPE = "application/json"
REVALIDATE = "private, no-cache"


class ApiConditionalGetMiddleware(ConditionalGetMiddleware):
    """Limit conditional responses to successful JSON API reads."""

    def process_response(
        self, request: HttpRequest, response: HttpResponseBase
    ) -> HttpResponseBase:
        """Tag a JSON read and return 304 when the client already has it."""
        if (
            request.method != "GET"
            or not request.path_info.startswith(API_PREFIX)
            or response.status_code != HTTPStatus.OK
            or response.streaming
            or not response.get("Content-Type", "").startswith(JSON_CONTENT_TYPE)
        ):
            return response
        if not response.has_header("Cache-Control"):
            response["Cache-Control"] = REVALIDATE
        return super().process_response(request, response)
