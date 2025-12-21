"""Unit tests for request timing / slow request surfacing middleware.

These tests are intentionally "unit-ish": they exercise the middleware in
isolation (RequestFactory + dummy view) to keep them deterministic and fast.
"""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus

from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory
import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.kwt_common.middleware import request_timing


def _perf_counter_sequence(
    monkeypatch: pytest.MonkeyPatch,
    values: list[float],
) -> None:
    """Patch perf_counter to return values in order."""
    it = iter(values)

    def _fake() -> float:
        return next(it)

    monkeypatch.setattr(request_timing.time, "perf_counter", _fake)


def _make_middleware(
    get_response: Callable[[HttpRequest], HttpResponse],
) -> request_timing.RequestTimingMiddleware:
    """Construct middleware around the given get_response."""
    return request_timing.RequestTimingMiddleware(get_response)


def test_append_server_timing_appends_comma_separated() -> None:
    """Server-Timing header values should combine using comma separation."""
    assert request_timing._append_server_timing(None, "app;dur=1") == "app;dur=1"
    assert request_timing._append_server_timing("a", "b") == "a, b"


def test_request_timing_adds_headers_even_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> None:
    """Even when slow-request logging is disabled, timing headers are always set."""
    settings.KORFBAL_LOG_SLOW_REQUESTS = False

    _perf_counter_sequence(monkeypatch, [1.0, 1.123])

    expected_elapsed_ms = 123

    def view(_request: HttpRequest) -> HttpResponse:
        return HttpResponse("ok")

    mw = _make_middleware(view)

    request = RequestFactory().get("/health/")
    request.user = AnonymousUser()  # type: ignore[assignment]

    response = mw(request)

    assert response.status_code == HTTPStatus.OK
    assert response["X-Korfbal-Request-Duration-Ms"] == str(expected_elapsed_ms)
    assert f"app;dur={expected_elapsed_ms}" in response["Server-Timing"]
    assert "X-Korfbal-Slow-Request" not in response


def test_request_timing_marks_slow_and_buffers_when_threshold_met(
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> None:
    """When duration exceeds the threshold, the middleware should buffer an entry."""
    settings.KORFBAL_LOG_SLOW_REQUESTS = True
    settings.KORFBAL_SLOW_REQUEST_MS = 50
    settings.KORFBAL_SLOW_REQUEST_BUFFER_SIZE = 10

    start_t = 10.0
    end_t = 10.200
    _perf_counter_sequence(monkeypatch, [start_t, end_t])

    expected_elapsed_ms = int((end_t - start_t) * 1000)
    expected_status = HTTPStatus.CREATED

    saved: dict[str, object] = {}

    def fake_get(_key: str) -> list[dict[str, object]]:
        return []

    def fake_set(key: str, value: object, *, timeout: int) -> None:
        saved["key"] = key
        saved["value"] = value
        saved["timeout"] = timeout

    monkeypatch.setattr(request_timing.cache, "get", fake_get)
    monkeypatch.setattr(request_timing.cache, "set", fake_set)

    def view(_request: HttpRequest) -> HttpResponse:
        return HttpResponse("ok", status=expected_status)

    mw = _make_middleware(view)

    request = RequestFactory().post("/api/x")
    request.user = AnonymousUser()  # type: ignore[assignment]

    response = mw(request)

    assert response.status_code == expected_status
    assert response.get("X-Korfbal-Slow-Request") == "1"

    assert saved["key"] == "korfbal:slow_requests"
    value = saved["value"]
    assert isinstance(value, list)
    assert len(value) == 1
    entry = value[0]

    assert entry["method"] == "POST"
    assert entry["path"] == "/api/x"
    assert entry["status"] == expected_status
    assert entry["duration_ms"] == expected_elapsed_ms


def test_request_timing_buffer_truncates_to_size(
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> None:
    """The rolling buffer should be capped to the configured size."""
    settings.KORFBAL_LOG_SLOW_REQUESTS = True
    settings.KORFBAL_SLOW_REQUEST_MS = 0
    settings.KORFBAL_SLOW_REQUEST_BUFFER_SIZE = 2

    _perf_counter_sequence(monkeypatch, [0.0, 0.010])

    existing = [
        {"path": "/old-1"},
        {"path": "/old-2"},
    ]

    saved: dict[str, object] = {}

    monkeypatch.setattr(request_timing.cache, "get", lambda _k: existing)
    monkeypatch.setattr(
        request_timing.cache,
        "set",
        lambda _k, v, *, timeout: saved.setdefault("value", v),
    )

    def view(_request: HttpRequest) -> HttpResponse:
        return HttpResponse("ok")

    response = _make_middleware(view)(RequestFactory().get("/new"))

    assert response.get("X-Korfbal-Slow-Request") == "1"

    value = saved["value"]
    assert isinstance(value, list)
    expected_size = 2
    assert len(value) == expected_size
    assert value[0]["path"] == "/new"


def test_request_timing_buffer_size_zero_skips_cache(
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> None:
    """A buffer size of 0 should disable buffering without disabling the header."""
    settings.KORFBAL_LOG_SLOW_REQUESTS = True
    settings.KORFBAL_SLOW_REQUEST_MS = 0
    settings.KORFBAL_SLOW_REQUEST_BUFFER_SIZE = 0

    _perf_counter_sequence(monkeypatch, [0.0, 0.010])

    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("cache should not be touched")

    monkeypatch.setattr(request_timing.cache, "get", boom)
    monkeypatch.setattr(request_timing.cache, "set", boom)

    def view(_request: HttpRequest) -> HttpResponse:
        return HttpResponse("ok")

    response = _make_middleware(view)(RequestFactory().get("/new"))

    assert response.get("X-Korfbal-Slow-Request") == "1"


def test_request_timing_cache_corruption_is_handled(
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> None:
    """Non-list cache values should be treated as an empty buffer."""
    settings.KORFBAL_LOG_SLOW_REQUESTS = True
    settings.KORFBAL_SLOW_REQUEST_MS = 0
    settings.KORFBAL_SLOW_REQUEST_BUFFER_SIZE = 5

    _perf_counter_sequence(monkeypatch, [0.0, 0.010])

    saved: dict[str, object] = {}

    monkeypatch.setattr(request_timing.cache, "get", lambda _k: "not-a-list")
    monkeypatch.setattr(
        request_timing.cache,
        "set",
        lambda _k, v, *, timeout: saved.setdefault("value", v),
    )

    def view(_request: HttpRequest) -> HttpResponse:
        return HttpResponse("ok")

    response = _make_middleware(view)(RequestFactory().get("/new"))

    assert response.get("X-Korfbal-Slow-Request") == "1"

    value = saved["value"]
    assert isinstance(value, list)
    assert value
    assert value[0]["path"] == "/new"


def test_request_timing_includes_slow_db_signal_in_headers_and_entry(
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> None:
    """When slow DB query info is present, it should be surfaced in headers/buffer."""
    settings.KORFBAL_LOG_SLOW_REQUESTS = True
    settings.KORFBAL_SLOW_REQUEST_MS = 0
    settings.KORFBAL_SLOW_REQUEST_BUFFER_SIZE = 5
    settings.KORFBAL_SLOW_DB_INCLUDE_SQL = True

    _perf_counter_sequence(monkeypatch, [0.0, 0.100])

    saved: dict[str, object] = {}

    monkeypatch.setattr(request_timing.cache, "get", lambda _k: [])
    monkeypatch.setattr(
        request_timing.cache,
        "set",
        lambda _k, v, *, timeout: saved.setdefault("value", v),
    )

    def view(req: HttpRequest) -> HttpResponse:
        req._korfbal_slow_queries = [  # type: ignore[attr-defined]
            {"ms": 40, "sql": "select 1"},
            {"ms": 60, "sql": "select 2"},
        ]
        return HttpResponse("ok")

    request = RequestFactory().get("/db")
    response = _make_middleware(view)(request)

    expected_query_count = 2
    expected_db_total_ms = 100
    expected_db_max_ms = 60

    assert response.get("X-Korfbal-Slow-Db-Query-Count") == str(expected_query_count)
    assert f"dbslow;dur={expected_db_total_ms}" in response["Server-Timing"]

    value = saved["value"]
    assert isinstance(value, list)
    entry = value[0]
    assert entry["slow_db_query_count"] == expected_query_count
    assert entry["slow_db_total_ms"] == expected_db_total_ms
    assert entry["slow_db_max_ms"] == expected_db_max_ms
    assert isinstance(entry["slow_db_queries"], list)


def test_request_timing_cache_exceptions_are_swallowed(
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> None:
    """Cache backend failures must not break user requests."""
    settings.KORFBAL_LOG_SLOW_REQUESTS = True
    settings.KORFBAL_SLOW_REQUEST_MS = 0
    settings.KORFBAL_SLOW_REQUEST_BUFFER_SIZE = 5

    _perf_counter_sequence(monkeypatch, [0.0, 0.010])

    monkeypatch.setattr(request_timing.cache, "get", lambda _k: [])

    def failing_set(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("cache backend is down")

    monkeypatch.setattr(request_timing.cache, "set", failing_set)

    def view(_request: HttpRequest) -> HttpResponse:
        return HttpResponse("ok")

    response = _make_middleware(view)(RequestFactory().get("/new"))

    # Middleware should not fail the request.
    assert response.status_code == HTTPStatus.OK
    assert response.get("X-Korfbal-Slow-Request") == "1"
