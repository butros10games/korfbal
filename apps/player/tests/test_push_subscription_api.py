"""Tests for web push subscription endpoints."""

from __future__ import annotations

from http import HTTPStatus
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.test.client import Client
import pytest

from apps.player.models.push_subscription import PlayerPushSubscription


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_push_subscriptions_requires_authentication(client: Client) -> None:
    """Anonymous requests cannot manage push subscriptions."""
    response = client.get("/api/player/me/push-subscriptions/")
    assert response.status_code in {HTTPStatus.FORBIDDEN, HTTPStatus.UNAUTHORIZED}


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_push_subscriptions_register_list_and_deactivate(client: Client) -> None:
    """A user can register, list, upsert and deactivate push subscriptions."""
    user = get_user_model().objects.create_user(
        username="push_user",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(user)

    response_empty = client.get("/api/player/me/push-subscriptions/")
    assert response_empty.status_code == HTTPStatus.OK
    assert response_empty.json() == []

    payload = {
        "subscription": {
            "endpoint": "https://example.com/push/endpoint-1",
            "keys": {"p256dh": "abc", "auth": "def"},
        },
        "user_agent": "pytest",
    }

    response_create = client.post(
        "/api/player/me/push-subscriptions/",
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert response_create.status_code == HTTPStatus.CREATED
    created_payload = response_create.json()
    assert created_payload["created"] is True
    assert (
        created_payload["subscription"]["endpoint"]
        == payload["subscription"]["endpoint"]
    )
    assert created_payload["subscription"]["is_active"] is True

    response_list = client.get("/api/player/me/push-subscriptions/")
    assert response_list.status_code == HTTPStatus.OK
    items = response_list.json()
    assert isinstance(items, list)
    assert len(items) == 1
    assert items[0]["endpoint"] == payload["subscription"]["endpoint"]

    # Upsert should return 200 and keep it active.
    response_update = client.post(
        "/api/player/me/push-subscriptions/",
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert response_update.status_code == HTTPStatus.OK
    update_payload = response_update.json()
    assert update_payload["created"] is False
    assert (
        update_payload["subscription"]["endpoint"]
        == payload["subscription"]["endpoint"]
    )

    response_delete = client.delete(
        "/api/player/me/push-subscriptions/",
        data=json.dumps({"endpoint": payload["subscription"]["endpoint"]}),
        content_type="application/json",
    )
    assert response_delete.status_code == HTTPStatus.NO_CONTENT

    response_after = client.get("/api/player/me/push-subscriptions/")
    assert response_after.status_code == HTTPStatus.OK
    assert response_after.json() == []


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_push_subscriptions_validates_payload(client: Client) -> None:
    """Invalid subscription payloads are rejected with 400."""
    user = get_user_model().objects.create_user(
        username="push_user_invalid",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(user)

    response = client.post(
        "/api/player/me/push-subscriptions/",
        data=json.dumps({"subscription": {"endpoint": "https://example.com"}}),
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_push_test_endpoint_requires_authentication(client: Client) -> None:
    """Anonymous requests cannot send debug test push notifications."""
    response = client.post("/api/player/me/push-subscriptions/test/")
    assert response.status_code in {HTTPStatus.FORBIDDEN, HTTPStatus.UNAUTHORIZED}


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_push_test_endpoint_requires_staff(client: Client) -> None:
    """Only staff users can call the debug test push endpoint."""
    user = get_user_model().objects.create_user(
        username="push_user_non_staff",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(user)

    response = client.post("/api/player/me/push-subscriptions/test/")
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json()["detail"]


@pytest.mark.django_db
@override_settings(
    SECURE_SSL_REDIRECT=False,
    WEBPUSH_VAPID_PUBLIC_KEY="",
    WEBPUSH_VAPID_PRIVATE_KEY="",
    WEBPUSH_VAPID_SUBJECT="",
)
def test_push_test_endpoint_requires_webpush_configuration(client: Client) -> None:
    """The endpoint returns 409 when VAPID keys are not configured."""
    user = get_user_model().objects.create_user(
        username="push_user_staff_missing_config",
        password="pass1234",  # noqa: S106  # nosec
    )
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    client.force_login(user)

    response = client.post("/api/player/me/push-subscriptions/test/")
    assert response.status_code == HTTPStatus.CONFLICT
    payload = response.json()
    assert payload["detail"] == "Web push not configured"
    assert "missing" in payload


@pytest.mark.django_db
@override_settings(
    SECURE_SSL_REDIRECT=False,
    WEBPUSH_VAPID_PUBLIC_KEY="dummy",
    WEBPUSH_VAPID_PRIVATE_KEY="dummy",
    WEBPUSH_VAPID_SUBJECT="mailto:test@example.com",
)
def test_push_test_endpoint_requires_active_subscriptions(client: Client) -> None:
    """The endpoint returns 400 when the user has no active subscriptions."""
    user = get_user_model().objects.create_user(
        username="push_user_staff_no_subs",
        password="pass1234",  # noqa: S106  # nosec
    )
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    client.force_login(user)

    response = client.post("/api/player/me/push-subscriptions/test/")
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == "No active push subscriptions"


@pytest.mark.django_db
@override_settings(
    SECURE_SSL_REDIRECT=False,
    WEBPUSH_VAPID_PUBLIC_KEY="dummy",
    WEBPUSH_VAPID_PRIVATE_KEY="dummy",
    WEBPUSH_VAPID_SUBJECT="mailto:test@example.com",
)
def test_push_test_endpoint_sends_to_all_active_subscriptions(client: Client) -> None:
    """Staff can send a test push to all of their active subscriptions."""
    user = get_user_model().objects.create_user(
        username="push_user_staff",
        password="pass1234",  # noqa: S106  # nosec
    )
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    client.force_login(user)

    PlayerPushSubscription.objects.create(
        user=user,
        endpoint="https://example.com/push/test-endpoint",
        subscription={
            "endpoint": "https://example.com/push/test-endpoint",
            "keys": {"p256dh": "abc", "auth": "def"},
        },
        is_active=True,
        user_agent="pytest",
    )

    with patch("apps.player.api.views.send_to_model_subscription") as mocked_send:
        response = client.post("/api/player/me/push-subscriptions/test/")

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload == {"total": 1, "sent": 1, "failed": 0}
    assert mocked_send.call_count == 1
