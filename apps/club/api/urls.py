"""URL routes for club API."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.player.media_paths import VARIANT

from .logo import ClubLogoAPIView, ClubLogoVariantAPIView
from .onboarding import (
    OnboardingClubSearchView,
    OnboardingClubTeamsView,
    OnboardingClubView,
    OnboardingKnkvLookupView,
    OnboardingPlayerView,
    OnboardingRequestView,
    OnboardingSpectatorView,
    OnboardingStateView,
)
from .views import ClubViewSet


router = DefaultRouter()
router.register(r"clubs", ClubViewSet)

urlpatterns = [
    path(
        "clubs/<uuid:club_id>/logo/<str:version>/",
        ClubLogoAPIView.as_view(),
        name="club-logo",
    ),
    path(
        f"clubs/<uuid:club_id>/logo/<str:version>/{VARIANT}/",
        ClubLogoVariantAPIView.as_view(),
        name="club-logo-variant",
    ),
    path("onboarding/", OnboardingStateView.as_view(), name="onboarding-state"),
    path(
        "onboarding/spectator/",
        OnboardingSpectatorView.as_view(),
        name="onboarding-spectator",
    ),
    path(
        "onboarding/knkv-lookup/",
        OnboardingKnkvLookupView.as_view(),
        name="onboarding-knkv-lookup",
    ),
    path(
        "onboarding/clubs/",
        OnboardingClubSearchView.as_view(),
        name="onboarding-clubs",
    ),
    path(
        "onboarding/clubs/<uuid:club_id>/teams/",
        OnboardingClubTeamsView.as_view(),
        name="onboarding-club-teams",
    ),
    path(
        "onboarding/player/", OnboardingPlayerView.as_view(), name="onboarding-player"
    ),
    path("onboarding/club/", OnboardingClubView.as_view(), name="onboarding-club"),
    path(
        "onboarding/requests/<uuid:request_id>/",
        OnboardingRequestView.as_view(),
        name="onboarding-request",
    ),
    path("", include(router.urls)),
]
