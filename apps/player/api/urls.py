"""URL routes for player API."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    CurrentPlayerAPIView,
    PlayerOverviewAPIView,
    PlayerStatsAPIView,
    PlayerViewSet,
)


router = DefaultRouter()
router.register(r"players", PlayerViewSet)

urlpatterns = [
    path("", include(router.urls)),
    path("me/", CurrentPlayerAPIView.as_view(), name="player-current"),
    path(
        "me/overview/",
        PlayerOverviewAPIView.as_view(),
        name="player-overview",
    ),
    path(
        "players/<uuid:player_id>/overview/",
        PlayerOverviewAPIView.as_view(),
        name="player-overview-detail",
    ),
    path(
        "me/stats/",
        PlayerStatsAPIView.as_view(),
        name="player-stats-current",
    ),
    path(
        "players/<uuid:player_id>/stats/",
        PlayerStatsAPIView.as_view(),
        name="player-stats",
    ),
]
