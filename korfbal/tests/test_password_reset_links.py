"""Native and web clients share reset links on the public frontend origin."""

from http import HTTPStatus
import secrets

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, override_settings
import pytest


@pytest.mark.django_db
def test_reset_email_uses_configured_frontend_not_inferred_api_host(
    client: Client,
) -> None:
    """A dedicated API hostname must not produce an unregistered web.api host."""
    User.objects.create_user(
        username="reset-link-test",
        email="reset-link@example.com",
        password=secrets.token_urlsafe(24),
    )
    with override_settings(
        ALLOWED_HOSTS=["api.korfbal.butrosgroot.com"],
        BG_AUTH_FRONTEND_BASE_URL="https://korfbal.butrosgroot.com",
    ):
        response = client.post(
            "/api/auth/password-reset/request/",
            {"email": "reset-link@example.com"},
            content_type="application/json",
            HTTP_HOST="api.korfbal.butrosgroot.com",
        )
    assert response.status_code == HTTPStatus.OK
    assert len(mail.outbox) == 1
    body = mail.outbox[0].body
    assert "https://korfbal.butrosgroot.com/password-reset/confirm/" in body
    assert "web.api.korfbal.butrosgroot.com" not in body
