"""Regression/edge-case tests for the slow-requests debug endpoint."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.cache import cache
import pytest
from pytest_django.fixtures import SettingsWrapper
from rest_framework import status
from rest_framework.test import APIClient


@pytest.fixture
def admin_client(db: None) -> APIClient:
    """Return an authenticated admin/staff API client."""
    user_model = get_user_model()
    admin = user_model.objects.create_user(
        username="admin2",
        email="admin2@example.com",
        is_staff=True,
        is_superuser=True,
    )
    admin.set_password("pass")
    admin.save()

    client = APIClient()
    client.force_authenticate(admin)
    return client


@pytest.fixture
def user_client(db: None) -> APIClient:
    """Return an authenticated non-staff API client."""
    user_model = get_user_model()
    user = user_model.objects.create_user(
        username="user1",
        email="user1@example.com",
        is_staff=False,
    )
    user.set_password("pass")
    user.save()

    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.mark.django_db
def test_slow_requests_endpoint_denies_non_staff(
    settings: SettingsWrapper,
    user_client: APIClient,
) -> None:
    """The endpoint is operational/admin-only; non-staff must be denied."""
    settings.KORFBAL_LOG_SLOW_REQUESTS = True

    resp = user_client.get("/api/debug/slow-requests/")
    assert resp.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
def test_slow_requests_endpoint_limit_parsing_and_bounds(
    settings: SettingsWrapper,
    admin_client: APIClient,
) -> None:
    """Regression: limit should be parsed safely and clamped to sane bounds."""
    settings.KORFBAL_LOG_SLOW_REQUESTS = True

    total_items = 600
    default_limit = 50
    min_limit = 1
    max_limit = 500

    items = [{"path": f"/p/{i}"} for i in range(total_items)]
    cache.set("korfbal:slow_requests", items, timeout=60)

    # Invalid -> default 50
    resp = admin_client.get("/api/debug/slow-requests/?limit=not-an-int")
    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["count"] == total_items
    assert len(body["items"]) == default_limit

    # Too small -> clamp to 1
    resp2 = admin_client.get("/api/debug/slow-requests/?limit=-10")
    assert resp2.status_code == status.HTTP_200_OK
    body2 = resp2.json()
    assert len(body2["items"]) == min_limit

    # Too large -> clamp to 500
    resp3 = admin_client.get("/api/debug/slow-requests/?limit=9999")
    assert resp3.status_code == status.HTTP_200_OK
    body3 = resp3.json()
    assert len(body3["items"]) == max_limit


@pytest.mark.django_db
def test_slow_requests_endpoint_handles_cache_corruption(
    settings: SettingsWrapper,
    admin_client: APIClient,
) -> None:
    """Corrupted cache values should not break the endpoint."""
    settings.KORFBAL_LOG_SLOW_REQUESTS = True

    cache.set("korfbal:slow_requests", "oops", timeout=60)

    resp = admin_client.get("/api/debug/slow-requests/")
    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["count"] == 0
    assert body["items"] == []


@pytest.mark.django_db
def test_slow_requests_endpoint_delete_clears_buffer(
    settings: SettingsWrapper,
    admin_client: APIClient,
) -> None:
    """DELETE should clear the buffer (useful during debugging)."""
    settings.KORFBAL_LOG_SLOW_REQUESTS = True

    cache.set(
        "korfbal:slow_requests",
        [{"path": "/x", "duration_ms": 123, "method": "GET"}],
        timeout=60,
    )

    resp = admin_client.delete("/api/debug/slow-requests/")
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["ok"] is True

    resp2 = admin_client.get("/api/debug/slow-requests/")
    assert resp2.status_code == status.HTTP_200_OK
    assert resp2.json()["items"] == []
