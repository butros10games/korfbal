"""Tests for page-visit registration behavior and model contracts."""

from datetime import timedelta

from django.contrib.auth.models import AnonymousUser, User
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory
from django.utils import timezone
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
def test_anonymous_visit_is_not_registered() -> None:
    """Anonymous traffic must not create player-linked tracking data."""
    response = _middleware()(_request("/hub/index/", user=AnonymousUser()))

    assert response.status_code == HTTP_STATUS_NO_CONTENT
    assert not PageConnectRegistration.objects.exists()


@pytest.mark.django_db
def test_authenticated_visit_creates_registration_and_resets_navigation() -> None:
    """An authenticated player's first visit stores the page and session state."""
    user = User.objects.create_user(
        username="tracked_player",
        password=TEST_PASSWORD,
    )
    player = Player.objects.get(user=user)
    request = _request("/hub/index/", user=user)

    response = _middleware()(request)

    assert response.status_code == HTTP_STATUS_NO_CONTENT
    registration = PageConnectRegistration.objects.get()
    assert registration.player == player
    assert registration.page == "/hub/index/"
    assert request.session["back_counter"] == 1


@pytest.mark.django_db
def test_repeat_visit_updates_one_registration_without_resetting_back_navigation() -> (
    None
):
    """Repeated visits update the same row and preserve explicit back navigation."""
    user = User.objects.create_user(
        username="repeat_tracked_player",
        password=TEST_PASSWORD,
    )
    middleware = _middleware()
    middleware(_request("/hub/index/", user=user))
    registration = PageConnectRegistration.objects.get()
    previous_registration_date = timezone.now() - timedelta(days=1)
    PageConnectRegistration.objects.filter(pk=registration.pk).update(
        registration_date=previous_registration_date
    )
    request = _request("/hub/index/", user=user)
    request.session["is_back_navigation"] = True
    request.session["back_counter"] = PRESERVED_BACK_COUNTER

    middleware(request)

    registration.refresh_from_db()
    assert PageConnectRegistration.objects.count() == 1
    assert registration.registration_date > previous_registration_date
    assert "is_back_navigation" not in request.session
    assert request.session["back_counter"] == PRESERVED_BACK_COUNTER


@pytest.mark.django_db
def test_authenticated_user_without_player_is_ignored() -> None:
    """Tracking should remain non-blocking if the optional profile is absent."""
    user = User.objects.create_user(
        username="tracked_user_without_player",
        password=TEST_PASSWORD,
    )
    Player.objects.filter(user=user).delete()

    response = _middleware()(_request("/hub/index/", user=user))

    assert response.status_code == HTTP_STATUS_NO_CONTENT
    assert not PageConnectRegistration.objects.exists()


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
