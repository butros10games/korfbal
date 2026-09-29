"""Tests for page-visit registration behavior and model contracts."""

from django.contrib.auth.models import AnonymousUser, User
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory
import pytest

from apps.hub.models import PageConnectRegistration
from apps.kwt_common.middleware import VisitorTrackingMiddleware
from apps.player.models import Player


TEST_PASSWORD = "pass1234"  # nosec B105 - test credential constant
HTTP_STATUS_NO_CONTENT = 204
PRESERVED_BACK_COUNTER = 7


def _request(path: str, *, user: User | AnonymousUser) -> HttpRequest:
    request = RequestFactory().get(path)
    request.user = user
    SessionMiddleware(lambda _request: HttpResponse()).process_request(request)
    return request


def _middleware() -> VisitorTrackingMiddleware:
    return VisitorTrackingMiddleware(
        lambda _request: HttpResponse(status=HTTP_STATUS_NO_CONTENT)
    )


@pytest.mark.django_db
def test_registration_model_validates_page_length_and_cascades_with_player() -> None:
    """The stored page is bounded and registrations are owned by their player."""
    user = User.objects.create_user(
        username="registration_model_player",
        password=TEST_PASSWORD,
    )
    player = Player.objects.get(user=user)
    registration = PageConnectRegistration(player=player, page="x" * 256)

    with pytest.raises(ValidationError, match="at most 255 characters"):
        registration.full_clean()

    registration.page = "/teams/fixture/"
    registration.full_clean()
    registration.save()
    assert str(registration) == "registration_model_player - /teams/fixture/"

    player.delete()
    assert not PageConnectRegistration.objects.exists()
