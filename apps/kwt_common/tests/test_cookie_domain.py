"""Session and CSRF cookies follow the domain the request arrived on."""

from __future__ import annotations

from django.conf import settings
from django.test import Client, override_settings
import pytest

from apps.kwt_common.middleware.cookie_domain import cookie_domain_for_host


COOKIE_DOMAINS = [".korfbal.butrosgroot.com", ".korfconnect.nl"]


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("api.korfconnect.nl", ".korfconnect.nl"),
        ("korfconnect.nl:443", ".korfconnect.nl"),
        ("api.korfbal.butrosgroot.com", ".korfbal.butrosgroot.com"),
        ("notkorfconnect.nl", None),
        ("localhost", None),
    ],
)
def test_cookie_domain_for_host(host: str, expected: str | None) -> None:
    """Only exact domains and their subdomains match."""
    assert cookie_domain_for_host(host, COOKIE_DOMAINS) == expected


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("host", "expected_domain"),
    [
        ("api.korfconnect.nl", ".korfconnect.nl"),
        ("api.korfbal.butrosgroot.com", ".korfbal.butrosgroot.com"),
    ],
)
@override_settings(
    ALLOWED_HOSTS=["api.korfconnect.nl", "api.korfbal.butrosgroot.com"],
    CSRF_COOKIE_DOMAIN=".korfbal.butrosgroot.com",
    KORFBAL_COOKIE_DOMAINS=COOKIE_DOMAINS,
)
def test_csrf_cookie_is_scoped_to_request_domain(
    client: Client, host: str, expected_domain: str
) -> None:
    """A cookie scoped to the old domain would be rejected on the new one."""
    response = client.get("/api/auth/session/", HTTP_HOST=host)

    assert response.cookies[settings.CSRF_COOKIE_NAME]["domain"] == expected_domain
