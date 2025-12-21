"""Tests for slow request surfacing."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.cache import cache
import pytest
from pytest_django.fixtures import SettingsWrapper
from rest_framework import status
from rest_framework.test import APIClient


@pytest.mark.django_db
def test_request_timing_headers_present(settings: SettingsWrapper) -> None:
    """Middleware should add timing headers and record slow requests when enabled."""
    settings.KORFBAL_LOG_SLOW_REQUESTS = True
    settings.KORFBAL_SLOW_REQUEST_MS = 0
    settings.KORFBAL_SLOW_REQUEST_BUFFER_SIZE = 50

    cache.set("korfbal:slow_requests", [], timeout=60)

    client = APIClient()
    resp = client.get("/api/schema/")

    assert resp.status_code == status.HTTP_200_OK
    assert "X-Korfbal-Request-Duration-Ms" in resp
    assert "Server-Timing" in resp
    # Because threshold is 0ms, everything counts as slow.
    assert resp.get("X-Korfbal-Slow-Request") == "1"

    items = cache.get("korfbal:slow_requests")
    assert isinstance(items, list)
    assert items
    assert items[0]["path"] == "/api/schema/"


@pytest.mark.django_db
def test_slow_requests_endpoint_requires_staff(settings: SettingsWrapper) -> None:
    """The slow-requests endpoint should be restricted to staff users."""
    settings.KORFBAL_LOG_SLOW_REQUESTS = True
    settings.KORFBAL_SLOW_REQUEST_MS = 0
    settings.KORFBAL_SLOW_REQUEST_BUFFER_SIZE = 10

    cache.set(
        "korfbal:slow_requests",
        [{"path": "/api/schema/", "duration_ms": 123, "method": "GET"}],
        timeout=60,
    )

    client = APIClient()

    # Unauthenticated should be blocked.
    resp = client.get("/api/debug/slow-requests/")
    assert resp.status_code in {401, 403}

    user_model = get_user_model()
    admin = user_model.objects.create_user(
        username="admin",
        email="admin@example.com",
        is_staff=True,
        is_superuser=True,
    )
    admin.set_password("pass")
    admin.save()
    client.force_authenticate(admin)

    resp2 = client.get("/api/debug/slow-requests/?limit=5")
    assert resp2.status_code == status.HTTP_200_OK
    body = resp2.json()
    assert body["count"] >= 1
    assert any(item.get("path") == "/api/schema/" for item in body["items"])
