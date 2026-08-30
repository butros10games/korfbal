"""Tests for shared template context and visitor tracking behavior."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory
from django.utils import timezone
import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.hub.models import PageConnectRegistration
from apps.kwt_common.context_processors.standard_imports import standard_imports
from apps.kwt_common.middleware.visitor_tracking import VisitorTrackingMiddleware
from apps.player.models import Player


def _response(_request: HttpRequest) -> HttpResponse:
    return HttpResponse("ok")


def test_standard_imports_returns_empty_profile_for_anonymous_user() -> None:
    """Anonymous templates must not receive a player profile URL."""
    request = RequestFactory().get("/")
    request.user = AnonymousUser()  # type: ignore[assignment]

    assert standard_imports(request) == {
        "profile_url": None,
        "profile_img_url": None,
    }


@pytest.mark.django_db
def test_standard_imports_returns_empty_profile_when_user_has_no_player() -> None:
    """An authenticated account without a Player remains a valid request."""
    user = get_user_model().objects.create_user(username="no-profile-context")
    Player.objects.filter(user=user).delete()
    request = RequestFactory().get("/")
    request.user = user  # type: ignore[assignment]

    assert standard_imports(request) == {
        "profile_url": None,
        "profile_img_url": None,
    }


@pytest.mark.django_db
def test_standard_imports_exposes_existing_player_links(
    settings: SettingsWrapper,
) -> None:
    """Existing players receive their SPA and profile-image links."""
    settings.WEB_APP_ORIGIN = "https://web.example.test"
    settings.STATIC_URL = "static/"
    user = get_user_model().objects.create_user(username="profile-context")
    player = user.player
    request = RequestFactory().get("/")
    request.user = user  # type: ignore[assignment]

    assert standard_imports(request) == {
        "profile_url": f"https://web.example.test/players/{player.id_uuid}",
        "profile_img_url": ("https://static/images/player/blank-profile-picture.png"),
    }


def test_visitor_tracking_ignores_anonymous_requests() -> None:
    """Public traffic must pass through without a database-backed player."""
    request = RequestFactory().get("/public/")
    request.user = AnonymousUser()  # type: ignore[assignment]

    response = VisitorTrackingMiddleware(_response)(request)

    assert response.status_code == HTTPStatus.OK


@pytest.mark.django_db
def test_visitor_tracking_creates_page_and_initializes_back_counter() -> None:
    """A first visit records the path and initializes navigation state."""
    user = get_user_model().objects.create_user(username="visitor-new")
    request = RequestFactory().get("/clubs/example/")
    request.user = user  # type: ignore[assignment]
    request.session = SessionStore()

    response = VisitorTrackingMiddleware(_response)(request)

    assert response.status_code == HTTPStatus.OK
    assert PageConnectRegistration.objects.filter(
        player=user.player,
        page="/clubs/example/",
    ).exists()
    assert request.session["back_counter"] == 1


@pytest.mark.django_db
def test_visitor_tracking_refreshes_existing_page_but_preserves_back_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated visit refreshes time without resetting back navigation."""
    user = get_user_model().objects.create_user(username="visitor-returning")
    page = PageConnectRegistration.objects.create(
        player=user.player,
        page="/teams/example/",
    )
    old_registration = timezone.now() - timedelta(days=1)
    PageConnectRegistration.objects.filter(pk=page.pk).update(
        registration_date=old_registration
    )
    refreshed_at = timezone.now()
    monkeypatch.setattr(
        "apps.kwt_common.middleware.visitor_tracking.timezone.now",
        lambda: refreshed_at,
    )
    request = RequestFactory().get("/teams/example/")
    request.user = user  # type: ignore[assignment]
    request.session = SessionStore()
    request.session["is_back_navigation"] = True
    request.session["back_counter"] = 7

    VisitorTrackingMiddleware(_response)(request)

    page.refresh_from_db()
    assert page.registration_date == refreshed_at
    assert dict(request.session) == {"back_counter": 7}


@pytest.mark.django_db
def test_visitor_tracking_still_serves_authenticated_user_without_player() -> None:
    """Missing profile data must never block an authenticated request."""
    user = get_user_model().objects.create_user(username="visitor-no-player")
    Player.objects.filter(user=user).delete()
    request = RequestFactory().get("/profile/")
    request.user = user  # type: ignore[assignment]
    request.session = SessionStore()

    response = VisitorTrackingMiddleware(_response)(request)

    assert response.status_code == HTTPStatus.OK
    assert PageConnectRegistration.objects.count() == 0
