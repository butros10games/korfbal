"""Regression tests for CORS behavior on public endpoints.

Anonymous users should be able to browse public pages (Home, Search, Teams)
from common frontend host variants such as:
- https://korfbal.butrosgroot.com
- https://www.korfbal.butrosgroot.com
- https://web.korfbal.butrosgroot.com

If these origins are not allowed, browsers will block API calls and the UI
appears empty.
"""

from __future__ import annotations

from http import HTTPStatus

from django.test import override_settings
from django.test.client import Client
import pytest


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_public_matches_endpoint_allows_www_origin(client: Client) -> None:
    """Public match endpoints should allow the www frontend origin."""
    response = client.get(
        "/api/matches/finished/?limit=1",
        HTTP_ORIGIN="https://www.korfbal.butrosgroot.com",
    )

    assert response.status_code == HTTPStatus.OK
    assert response.headers.get("Access-Control-Allow-Origin") == (
        "https://www.korfbal.butrosgroot.com"
    )
    assert response.headers.get("Access-Control-Allow-Credentials") == "true"


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_public_matches_endpoint_allows_web_subdomain_origin(client: Client) -> None:
    """Public match endpoints should allow the web.<domain> frontend origin."""
    response = client.get(
        "/api/matches/finished/?limit=1",
        HTTP_ORIGIN="https://web.korfbal.butrosgroot.com",
    )

    assert response.status_code == HTTPStatus.OK
    assert response.headers.get("Access-Control-Allow-Origin") == (
        "https://web.korfbal.butrosgroot.com"
    )
    assert response.headers.get("Access-Control-Allow-Credentials") == "true"
