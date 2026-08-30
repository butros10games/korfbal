"""Authentication, validation, and error contracts for hub APIs."""

from django.contrib.auth.models import User
from django.test.client import Client
from django.urls import reverse
import pytest


TEST_PASSWORD = "pass1234"  # nosec B105 - test credential constant
HTTP_STATUS_BAD_REQUEST = 400
HTTP_STATUS_UNAUTHORIZED = 401
HTTP_STATUS_METHOD_NOT_ALLOWED = 405


@pytest.mark.django_db
@pytest.mark.parametrize("url_name", ["index", "api_catalog_data"])
def test_protected_hub_api_returns_json_for_anonymous_requests(
    client: Client,
    url_name: str,
) -> None:
    """Protected APIs must not redirect anonymous clients to an HTML login page."""
    response = client.get(reverse(url_name), secure=True)

    assert response.status_code == HTTP_STATUS_UNAUTHORIZED
    assert response.headers["Content-Type"].startswith("application/json")
    assert "detail" in response.json()
    assert "Location" not in response.headers


@pytest.mark.django_db
def test_catalog_data_rejects_malformed_json(client: Client) -> None:
    """Malformed request bodies should use DRF's structured JSON error contract."""
    user = User.objects.create_user(
        username="catalog_malformed_json",
        password=TEST_PASSWORD,
    )
    client.force_login(user)

    response = client.post(
        reverse("api_catalog_data"),
        data='{"value":',
        content_type="application/json",
        secure=True,
    )

    assert response.status_code == HTTP_STATUS_BAD_REQUEST
    assert response.headers["Content-Type"].startswith("application/json")
    assert "detail" in response.json()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("url_name", "method"),
    [("index", "post"), ("api_catalog_data", "get")],
)
def test_hub_api_rejects_unsupported_methods_with_json(
    client: Client,
    url_name: str,
    method: str,
) -> None:
    """Wrong HTTP methods should return a machine-readable 405 response."""
    user = User.objects.create_user(
        username=f"hub_wrong_method_{method}",
        password=TEST_PASSWORD,
    )
    client.force_login(user)

    response = getattr(client, method)(reverse(url_name), secure=True)

    assert response.status_code == HTTP_STATUS_METHOD_NOT_ALLOWED
    assert response.headers["Content-Type"].startswith("application/json")
    assert "detail" in response.json()
