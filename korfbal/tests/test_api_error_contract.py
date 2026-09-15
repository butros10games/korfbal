"""Exercise shared HTTP error behavior across the registered API surface."""

from __future__ import annotations

from http import HTTPStatus
import json
import re

from django.contrib.auth.models import User
from django.http import HttpResponse, JsonResponse
from django.test import Client, RequestFactory
from django.urls import URLPattern, URLResolver
import pytest

from apps.game_tracker.api.urls import urlpatterns as tracker_urlpatterns
from korfbal.api_errors import ApiErrorResponseMiddleware, csrf_failure
from korfbal.api_urls import urlpatterns


UUID_VALUE = "11111111-1111-4111-8111-111111111111"


def _api_routes(
    patterns: list[URLPattern | URLResolver], prefix: str = ""
) -> list[str]:
    """Walk concrete routes, retaining duplicate names and unnamed legacy aliases."""
    routes = []
    for entry in patterns:
        pattern = entry.pattern.regex.pattern
        if "format" in entry.pattern.regex.groupindex:
            continue
        suffix = (
            re
            .sub(
                r"\(\?P<(\w+)>[^)]+\)",
                lambda match: (
                    "1"
                    if match[1] in {"member_id", "round_number", "pk"}
                    else UUID_VALUE
                ),
                pattern,
            )
            .removeprefix("^")
            .removesuffix("$")
            .removesuffix(r"\Z")
        )
        route = prefix + re.sub(r"\\(.)", r"\1", suffix)
        if isinstance(entry, URLResolver):
            routes.extend(_api_routes(list(entry.url_patterns), route))
        else:
            routes.append("/" + route)
    return sorted(set(routes))


API_ROUTES = sorted(
    set(
        _api_routes(urlpatterns)
        + _api_routes(urlpatterns, "api/")
        + _api_routes(tracker_urlpatterns, "match/api/")
    )
)


@pytest.mark.django_db
@pytest.mark.parametrize("url", API_ROUTES)
@pytest.mark.parametrize("authenticated", [False, True])
def test_registered_route_rejects_unsupported_method_as_json(
    client: Client, url: str, authenticated: bool
) -> None:
    """Every API family must reject TRACE without redirects, HTML or server errors."""
    if authenticated:
        client.force_login(
            User.objects.create_user(username="method-probe", is_staff=True)
        )
    response = client.generic("TRACE", url)
    assert response.status_code in {
        HTTPStatus.UNAUTHORIZED,
        HTTPStatus.FORBIDDEN,
        HTTPStatus.METHOD_NOT_ALLOWED,
    }, (url, response.status_code)
    assert response["Content-Type"] == "application/json"
    payload = response.json()
    assert isinstance(payload["code"], str)
    assert payload["code"]
    assert isinstance(payload["message"], str)
    assert payload["message"]
    if response.status_code == HTTPStatus.METHOD_NOT_ALLOWED:
        assert response["Allow"]


@pytest.mark.parametrize("prefix", ["", "/api", "/match/api"])
def test_unknown_api_route_has_json_not_found(client: Client, prefix: str) -> None:
    """Routing failures share the error contract, including legacy prefixes."""
    response = client.get(f"{prefix}/does-not-exist/", HTTP_ACCEPT="text/html")
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response["Content-Type"] == "application/json"
    assert response.json()["code"] == "not_found"
    assert response.json()["message"]


def test_csrf_failure_has_json_permission_denied() -> None:
    """Django's CSRF middleware must not send an HTML login error to API clients."""
    response = Client(enforce_csrf_checks=True).post(
        "/auth/login/", data={}, content_type="application/json"
    )
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response["Content-Type"] == "application/json"
    assert response.json()["code"] == "csrf_failed"


@pytest.mark.parametrize(
    ("status_code", "payload", "headers"),
    [
        (401, {"detail": "Invalid access token"}, {"WWW-Authenticate": "Bearer"}),
        (405, {}, {"Allow": "GET, POST"}),
        (429, {"detail": "Try again later."}, {"Retry-After": "30"}),
        (400, {"name": ["This field is required."]}, {}),
        (409, {"code": "revision_conflict", "live_revision": 12}, {}),
    ],
)
def test_error_contract_preserves_fields_and_protocol_headers(
    status_code: int, payload: dict, headers: dict
) -> None:
    """Clients retain field errors, revision metadata, challenges and retry hints."""
    original = JsonResponse(payload, status=status_code, headers=headers)
    response = ApiErrorResponseMiddleware(lambda request: original)(
        RequestFactory().get("/matches/")
    )
    actual = json.loads(response.content)
    for key, value in payload.items():
        assert actual[key] == value
    assert actual["message"]
    for key, value in headers.items():
        assert response[key] == value
    assert response["Cache-Control"] == "no-store"


def test_unexpected_server_error_does_not_expose_exception_details() -> None:
    """Unexpected server errors must not expose private tracebacks."""
    original = HttpResponse("private traceback", status=500)
    response = ApiErrorResponseMiddleware(lambda request: original)(
        RequestFactory().get("/matches/")
    )
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert b"private traceback" not in response.content
    assert b"server_error" in response.content


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["post", "patch"])
def test_malformed_json_and_invalid_media_type_are_distinct(
    client: Client, method: str
) -> None:
    """Parsing errors are 400 and unsupported input media types are 415."""
    user = User.objects.create_user(username="error-contract", is_staff=True)
    client.force_login(user)
    url = "/club/clubs/" if method == "post" else "/player/me/"
    malformed = getattr(client, method)(url, data="{", content_type="application/json")
    assert malformed.status_code == HTTPStatus.BAD_REQUEST
    assert malformed.json()["code"] == "bad_request"
    unsupported = getattr(client, method)(
        url, data="<xml/>", content_type="application/xml"
    )
    assert unsupported.status_code == HTTPStatus.UNSUPPORTED_MEDIA_TYPE
    assert unsupported.json()["code"] == "unsupported_media_type"


@pytest.mark.django_db
@pytest.mark.parametrize("url", API_ROUTES)
@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
def test_registered_routes_handle_missing_resources_and_non_object_input(
    client: Client, url: str, method: str
) -> None:
    """Probe dispatch with an account, empty catalogue, and non-object bodies."""
    client.force_login(User.objects.create_user(username="input-probe", is_staff=True))
    response = client.generic(
        method,
        url,
        data="[]" if method != "GET" else "",
        content_type="application/json",
    )
    assert response.status_code < HTTPStatus.INTERNAL_SERVER_ERROR or (
        response.status_code == HTTPStatus.SERVICE_UNAVAILABLE and "/spotify/" in url
    ), (url, method, response.status_code)
    if response.status_code >= HTTPStatus.BAD_REQUEST:
        assert response["Content-Type"] == "application/json"
        assert response.json()["code"]
        assert response.json()["message"]


@pytest.mark.parametrize("header", ["Bearer", "Bearer   ", "Bearer invalid"])
@pytest.mark.parametrize("url", ["/club/clubs/", "/player/me/"])
def test_invalid_bearer_credentials_are_not_silently_treated_as_anonymous(
    client: Client, header: str, url: str
) -> None:
    """Explicit invalid credentials receive 401 even on publicly readable routes."""
    response = client.get(url, HTTP_AUTHORIZATION=header)
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response["WWW-Authenticate"] == "Bearer"
    assert response.json()["detail"] == "Invalid access token"


def test_admin_csrf_errors_keep_the_html_admin_contract() -> None:
    """The API failure view must not replace the admin's HTML recovery page."""
    response = csrf_failure(RequestFactory().post("/admin/login/"))
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response["Content-Type"].startswith("text/html")


def test_validation_summary_keeps_the_actionable_field_message() -> None:
    """Adding a summary must not hide field errors from clients that prefer detail."""
    original = JsonResponse(
        {"username": ["This username is already in use."]}, status=400
    )
    response = ApiErrorResponseMiddleware(lambda request: original)(
        RequestFactory().post("/player/me/")
    )
    payload = json.loads(response.content)
    assert payload["username"] == ["This username is already in use."]
    assert payload["detail"] == "username: This username is already in use."


def test_message_field_validation_keeps_a_readable_summary() -> None:
    """A field named message must retain its errors and still get a text detail."""
    original = JsonResponse({"message": ["Not a valid string."]}, status=400)
    response = ApiErrorResponseMiddleware(lambda request: original)(
        RequestFactory().post("/audit/events/ingest/")
    )
    payload = json.loads(response.content)
    assert payload["message"] == ["Not a valid string."]
    assert payload["detail"] == "message: Not a valid string."
