"""JSON error responses for the dedicated API, including Django-level failures."""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
import json
from typing import Any

from django.http import HttpRequest, HttpResponse, HttpResponseBase, JsonResponse
from django.utils.cache import patch_cache_control
from django.views.csrf import csrf_failure as django_csrf_failure
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response


ERROR_CODES = {
    400: "bad_request",
    401: "not_authenticated",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    409: "conflict",
    413: "request_too_large",
    415: "unsupported_media_type",
    423: "locked",
    429: "throttled",
    500: "server_error",
    502: "bad_gateway",
    503: "service_unavailable",
    504: "gateway_timeout",
}


def _field_message(value: object) -> str | None:
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, list):
        for item in value:
            if message := _field_message(item):
                return message
    if isinstance(value, dict):
        for field, item in value.items():
            if message := _field_message(item):
                return (
                    message
                    if field == "non_field_errors"
                    else f"{str(field).replace('_', ' ')}: {message}"
                )
    return None


def _message(payload: dict[str, Any], status_code: int) -> str:
    errors = payload.get("errors")
    values = [payload.get(key) for key in ("message", "detail", "error")]
    if isinstance(errors, list) and errors:
        values.append(errors[0])
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    fields = {
        key: value
        for key, value in payload.items()
        if key not in {"code", "status", "error_code"}
    }
    if message := _field_message(fields):
        return message
    defaults = {
        HTTPStatus.BAD_REQUEST: "Invalid request. Check the supplied fields.",
        HTTPStatus.UNAUTHORIZED: "Authentication required.",
        HTTPStatus.FORBIDDEN: "You do not have permission to perform this action.",
        HTTPStatus.NOT_FOUND: "The requested resource was not found.",
        HTTPStatus.METHOD_NOT_ALLOWED: "This HTTP method is not allowed.",
        HTTPStatus.INTERNAL_SERVER_ERROR: "An unexpected server error occurred.",
    }
    if status_code in defaults:
        return defaults[status_code]
    try:
        return HTTPStatus(status_code).description
    except ValueError:
        return "The request could not be completed."


class ApiErrorResponseMiddleware:
    """Keep existing error fields and headers while guaranteeing JSON errors.

    Admin, static assets and metrics retain their own response contracts. Errors
    from URL resolution, CSRF and method decorators pass through this boundary
    too; application exceptions still reach Django's logging/error reporting.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        """Store the next middleware handler."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        """Normalize completed error responses without changing their status."""
        response = self.get_response(request)
        if (
            response.status_code < HTTPStatus.BAD_REQUEST
            or not isinstance(response, HttpResponse)
            or request.path_info == "/admin"
            or request.path_info.startswith(("/admin/", "/static/", "/metrics"))
        ):
            return response

        payload: dict[str, Any] = {}
        if response.status_code != HTTPStatus.INTERNAL_SERVER_ERROR:
            if isinstance(response, Response) and isinstance(response.data, dict):
                payload = dict(response.data)
            elif (
                response.get("Content-Type", "").split(";", 1)[0] == "application/json"
            ):
                try:
                    data = json.loads(response.content)
                except (ValueError, UnicodeDecodeError):
                    data = None
                if isinstance(data, dict):
                    payload = data
                elif isinstance(data, list):
                    payload = {"errors": data}
        payload.setdefault(
            "code", ERROR_CODES.get(response.status_code, "request_failed")
        )
        summary = _message(payload, response.status_code)
        payload.setdefault("message", summary)
        payload.setdefault("detail", summary)
        if isinstance(response, Response):
            response.data = payload
        response.content = JSONRenderer().render(payload)
        response["Content-Type"] = "application/json"
        response["Content-Length"] = str(len(response.content))
        patch_cache_control(response, no_store=True)
        return response


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    """Explain how to recover from a CSRF failure without exposing internal reasons."""
    if request.path_info == "/admin" or request.path_info.startswith("/admin/"):
        return django_csrf_failure(request, reason=reason)
    return JsonResponse(
        {
            "code": "csrf_failed",
            "detail": "Security verification failed. Refresh the page and try again.",
        },
        status=HTTPStatus.FORBIDDEN,
    )
