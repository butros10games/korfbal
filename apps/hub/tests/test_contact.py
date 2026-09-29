"""Contact requests from the apps reach the support inbox with sane limits."""

from __future__ import annotations

from smtplib import SMTPException
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test.client import Client
from django.urls import reverse
import pytest


HTTP_ACCEPTED = 202
HTTP_BAD_REQUEST = 400
HTTP_TOO_MANY = 429
HTTP_UNAVAILABLE = 503
TEST_PASSWORD = "pass1234"  # nosec B105 - test credential constant
REQUESTS_PER_HOUR = 5


@pytest.fixture(autouse=True)
def _clear_ratelimit_cache() -> None:
    cache.clear()


def _post(client: Client, payload: dict[str, object]) -> object:
    return client.post(
        reverse("hub-contact"),
        data=payload,
        content_type="application/json",
        secure=True,
    )


@pytest.mark.django_db
def test_anonymous_contact_request_emails_support_with_reply_to(
    client: Client,
    settings: object,
) -> None:
    """A visitor message reaches the inbox with a reply-to and its diagnostics."""
    settings.BG_AUTH_SUPPORT_EMAIL = "support@example.test"
    response = _post(
        client,
        {
            "topic": "tracking",
            "message": "De wedstrijdklok loopt niet door na een time-out.",
            "name": "Coach Jansen",
            "email": "coach@example.test",
            "context": {"platform": "ios", "app_version": "1.0.2", "page": ""},
        },
    )

    assert response.status_code == HTTP_ACCEPTED
    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert sent.to == ["support@example.test"]
    assert sent.reply_to == ["coach@example.test"]
    assert sent.subject == "[KorfConnect] Wedstrijden bijhouden"
    assert "Coach Jansen <coach@example.test>" in sent.body
    assert "De wedstrijdklok loopt niet door" in sent.body
    assert "platform: ios" in sent.body
    assert "app_version: 1.0.2" in sent.body
    assert "page" not in sent.body.split("Context:")[1]


@pytest.mark.django_db
def test_signed_in_contact_request_uses_account_email(client: Client) -> None:
    """Signed-in players reply from their account address without typing it."""
    user = User.objects.create_user(
        username="speler",
        email="speler@example.test",
        password=TEST_PASSWORD,
    )
    client.force_login(user)

    response = _post(
        client,
        {"topic": "account", "message": "Mijn spelersprofiel klopt niet helemaal."},
    )

    assert response.status_code == HTTP_ACCEPTED
    sent = mail.outbox[0]
    assert sent.reply_to == ["speler@example.test"]
    assert "speler <speler@example.test>" in sent.body
    assert "Ingelogd: ja" in sent.body


@pytest.mark.django_db
def test_anonymous_contact_request_requires_email(client: Client) -> None:
    """Visitors must leave an address, otherwise nothing is sent."""
    response = _post(
        client,
        {"topic": "other", "message": "Een bericht zonder afzender."},
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert "email" in response.json()
    assert mail.outbox == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    "payload",
    [
        {"topic": "unknown", "message": "Lang genoeg bericht.", "email": "a@b.test"},
        {"topic": "bug", "message": "kort", "email": "a@b.test"},
        {"topic": "bug", "message": "Lang genoeg bericht.", "email": "geen-adres"},
    ],
)
def test_contact_request_rejects_invalid_payloads(
    client: Client,
    payload: dict[str, object],
) -> None:
    """Unknown topics, short messages and bad addresses are rejected."""
    response = _post(client, payload)

    assert response.status_code == HTTP_BAD_REQUEST
    assert mail.outbox == []


@pytest.mark.django_db
def test_contact_requests_are_limited_per_address(client: Client) -> None:
    """One address cannot flood the inbox."""
    payload = {
        "topic": "other",
        "message": "Hetzelfde bericht, opnieuw en opnieuw.",
        "email": "spam@example.test",
    }
    statuses = [
        _post(client, payload).status_code for _ in range(REQUESTS_PER_HOUR + 1)
    ]

    assert statuses[:REQUESTS_PER_HOUR] == [HTTP_ACCEPTED] * REQUESTS_PER_HOUR
    assert statuses[REQUESTS_PER_HOUR] == HTTP_TOO_MANY
    assert len(mail.outbox) == REQUESTS_PER_HOUR


@pytest.mark.django_db
def test_contact_request_reports_mail_failure(client: Client) -> None:
    """A mail outage returns a retryable error with the direct address."""
    with patch(
        "django.core.mail.EmailMessage.send",
        side_effect=SMTPException("mailbox unavailable"),
    ):
        response = _post(
            client,
            {
                "topic": "bug",
                "message": "De app sluit af bij het openen van een toernooi.",
                "email": "tester@example.test",
            },
        )

    assert response.status_code == HTTP_UNAVAILABLE
    assert "mail ons direct" in response.json()["detail"]


@pytest.mark.django_db
def test_contact_request_refuses_without_support_address(
    client: Client,
    settings: object,
) -> None:
    """Without a configured inbox the endpoint refuses instead of dropping mail."""
    settings.BG_AUTH_SUPPORT_EMAIL = ""
    response = _post(
        client,
        {
            "topic": "bug",
            "message": "Genoeg tekens voor een bericht.",
            "email": "a@b.test",
        },
    )

    assert response.status_code == HTTP_UNAVAILABLE
    assert mail.outbox == []
