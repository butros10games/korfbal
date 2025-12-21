"""Unit tests for SlowQueryLoggingMiddleware.

These tests validate the middleware's intent:
- When enabled, collect a small list of the slowest DB queries during a request.
- Optionally include SQL/params for deeper debugging.
- When disabled, do not add per-request state.

We run the middleware in isolation (RequestFactory + get_response) to keep query
collection deterministic.
"""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory
import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.kwt_common.middleware.slow_queries import SlowQueryLoggingMiddleware


def _make_middleware(
    get_response: Callable[[HttpRequest], HttpResponse],
) -> SlowQueryLoggingMiddleware:
    """Construct middleware around the given get_response."""
    return SlowQueryLoggingMiddleware(get_response)


@pytest.mark.django_db
def test_slow_query_logging_disabled_does_not_add_request_state(
    settings: SettingsWrapper,
) -> None:
    """When disabled, the middleware should not attach _korfbal_slow_queries."""
    settings.KORFBAL_LOG_SLOW_DB_QUERIES = False

    def view(_request: HttpRequest) -> HttpResponse:
        get_user_model().objects.count()
        return HttpResponse("ok")

    request = RequestFactory().get("/x")
    response = _make_middleware(view)(request)

    assert response.status_code == HTTPStatus.OK
    assert not hasattr(request, "_korfbal_slow_queries")


@pytest.mark.django_db
def test_slow_query_logging_collects_queries_when_enabled(
    settings: SettingsWrapper,
) -> None:
    """When enabled and threshold is 0ms, at least one query should be captured."""
    settings.KORFBAL_LOG_SLOW_DB_QUERIES = True
    settings.KORFBAL_SLOW_DB_QUERY_MS = 0
    settings.KORFBAL_SLOW_DB_INCLUDE_SQL = False

    def view(_request: HttpRequest) -> HttpResponse:
        get_user_model().objects.count()
        return HttpResponse("ok")

    request = RequestFactory().get("/x")
    response = _make_middleware(view)(request)

    assert response.status_code == HTTPStatus.OK

    slow_queries = getattr(request, "_korfbal_slow_queries", None)
    assert isinstance(slow_queries, list)
    assert slow_queries

    first = slow_queries[0]
    assert isinstance(first, dict)
    assert "ms" in first
    assert "alias" in first
    assert "sql" not in first


@pytest.mark.django_db
def test_slow_query_logging_includes_sql_when_enabled(
    settings: SettingsWrapper,
) -> None:
    """Including SQL should enrich payload entries with sql/params."""
    settings.KORFBAL_LOG_SLOW_DB_QUERIES = True
    settings.KORFBAL_SLOW_DB_QUERY_MS = 0
    settings.KORFBAL_SLOW_DB_INCLUDE_SQL = True

    def view(_request: HttpRequest) -> HttpResponse:
        get_user_model().objects.count()
        return HttpResponse("ok")

    request = RequestFactory().get("/x")
    response = _make_middleware(view)(request)

    assert response.status_code == HTTPStatus.OK

    slow_queries = getattr(request, "_korfbal_slow_queries", None)
    assert isinstance(slow_queries, list)
    assert slow_queries

    first = slow_queries[0]
    assert "sql" in first
    assert "params" in first
