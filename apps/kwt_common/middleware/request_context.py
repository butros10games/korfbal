"""Give every request an ID and write one structured access-log line for it.

The ID comes from a valid incoming ``X-Request-ID`` (so an edge or client can
correlate) or is generated, is echoed in the response and is attached to every
log record and Sentry event raised while the request runs.

The access line records the URL *pattern* rather than the requested path: paths
can carry activation tokens, password-reset IDs and other personal values.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import inspect
import logging
import time
from typing import cast
import uuid

from django.conf import settings
from django.http import HttpRequest, HttpResponseBase
from django.utils.decorators import sync_and_async_middleware
from korfbal.observability import (
    REQUEST_ID_HEADER,
    bind_request_id,
    valid_request_id,
)
import sentry_sdk


access_logger = logging.getLogger("korfbal.access")

SyncGetResponse = Callable[[HttpRequest], HttpResponseBase]
AsyncGetResponse = Callable[[HttpRequest], Awaitable[HttpResponseBase]]
GetResponse = SyncGetResponse | AsyncGetResponse
SERVER_ERROR = 500


def _start(request: HttpRequest) -> str:
    request_id = (
        valid_request_id(request.headers.get(REQUEST_ID_HEADER)) or uuid.uuid4().hex
    )
    # No-op until Sentry is initialized; tags only this request's isolation scope.
    sentry_sdk.set_tag("request_id", request_id)
    return request_id


def _finish(
    request: HttpRequest, response: HttpResponseBase, request_id: str, start: float
) -> HttpResponseBase:
    response[REQUEST_ID_HEADER] = request_id
    if not getattr(settings, "KORFBAL_ACCESS_LOG", False):
        return response
    resolver_match = getattr(request, "resolver_match", None)
    status = response.status_code
    access_logger.log(
        logging.WARNING if status >= SERVER_ERROR else logging.INFO,
        "%s %s %s",
        request.method,
        getattr(resolver_match, "view_name", None) or "unmatched",
        status,
        extra={
            "http_method": request.method,
            "route": getattr(resolver_match, "route", None),
            "view": getattr(resolver_match, "view_name", None),
            "status": status,
            "duration_ms": round((time.perf_counter() - start) * 1000, 1),
        },
    )
    return response


@sync_and_async_middleware
def request_context_middleware(get_response: GetResponse) -> GetResponse:
    """Bind a request ID for the request's duration and log its outcome."""
    if inspect.iscoroutinefunction(get_response):
        async_get_response = cast(AsyncGetResponse, get_response)

        async def async_middleware(request: HttpRequest) -> HttpResponseBase:
            start = time.perf_counter()
            request_id = _start(request)
            with bind_request_id(request_id):
                response = await async_get_response(request)
                return _finish(request, response, request_id, start)

        return async_middleware

    sync_get_response = cast(SyncGetResponse, get_response)

    def middleware(request: HttpRequest) -> HttpResponseBase:
        start = time.perf_counter()
        request_id = _start(request)
        with bind_request_id(request_id):
            response = sync_get_response(request)
            return _finish(request, response, request_id, start)

    return middleware
