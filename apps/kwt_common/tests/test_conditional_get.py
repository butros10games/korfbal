"""Unchanged API reads are answered without resending their body."""

from __future__ import annotations

from http import HTTPStatus

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.test import RequestFactory
from django.test.client import Client
import pytest

from apps.kwt_common.middleware.conditional_get import ApiConditionalGetMiddleware


ORIGIN = "https://www.korfbal.butrosgroot.com"
READ = "/api/matches/finished/?limit=1"


@pytest.mark.django_db
def test_unchanged_api_read_returns_empty_not_modified(client: Client) -> None:
    """A repeated read validates its tag and keeps the cross-origin headers."""
    first = client.get(READ, HTTP_ORIGIN=ORIGIN)

    assert first.status_code == HTTPStatus.OK
    assert first.headers["Cache-Control"] == "private, no-cache"
    tag = first.headers["ETag"]

    repeated = client.get(READ, HTTP_ORIGIN=ORIGIN, HTTP_IF_NONE_MATCH=tag)

    assert repeated.status_code == HTTPStatus.NOT_MODIFIED
    assert repeated.content == b""
    assert repeated.headers["ETag"] == tag
    assert repeated.headers["Access-Control-Allow-Origin"] == ORIGIN


@pytest.mark.django_db
def test_changed_api_read_returns_the_new_body(client: Client) -> None:
    """A tag from other content never suppresses the current response."""
    response = client.get(READ, HTTP_IF_NONE_MATCH='"another-body"')

    assert response.status_code == HTTPStatus.OK
    assert response.json() == []


def _process(path: str, response: HttpResponse, method: str = "GET") -> HttpResponse:
    request: HttpRequest = RequestFactory().generic(method, path)
    middleware = ApiConditionalGetMiddleware(lambda _request: response)
    result = middleware(request)
    assert isinstance(result, HttpResponse)
    return result


def test_declared_cache_policies_are_kept() -> None:
    """Credential and media reads that forbid storage get no validator."""
    response = JsonResponse({"token": "secret"})
    response["Cache-Control"] = "private, no-store"

    result = _process("/api/matches/1/tracker-access/", response)

    assert result.headers["Cache-Control"] == "private, no-store"
    assert "ETag" not in result.headers


@pytest.mark.parametrize(
    ("path", "method", "response"),
    [
        ("/api/matches/", "POST", JsonResponse({"created": True})),
        ("/admin/", "GET", HttpResponse("<html></html>")),
        ("/api/club/clubs/1/logo/v/", "GET", HttpResponse(b"png", "image/png")),
        ("/api/matches/1/", "GET", JsonResponse({"detail": "x"}, status=404)),
    ],
)
def test_other_responses_are_untouched(
    path: str, method: str, response: HttpResponse
) -> None:
    """Writes, pages, media and errors keep their headers."""
    result = _process(path, response, method)

    assert "ETag" not in result.headers
    assert "Cache-Control" not in result.headers
