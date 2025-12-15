"""URL routes for player API."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    CurrentPlayerAPIView,
    CurrentPlayerGoalSongAPIView,
    PlayerConnectedClubRecentResultsAPIView,
    PlayerOverviewAPIView,
    PlayerStatsAPIView,
    PlayerViewSet,
    SpotifyCallbackView,
    SpotifyConnectAPIView,
    SpotifyPlayAPIView,
    UploadGoalSongAPIView,
    UploadProfilePictureAPIView,
)


router = DefaultRouter()
router.register(r"players", PlayerViewSet)

urlpatterns = [
    path("", include(router.urls)),
    path("me/", CurrentPlayerAPIView.as_view(), name="player-current"),
    path(
        "me/goal-song/",
        CurrentPlayerGoalSongAPIView.as_view(),
        name="player-goal-song",
    ),
    path(
        "api/upload_profile_picture/",
        UploadProfilePictureAPIView.as_view(),
        name="player-upload-profile-picture",
    ),
    path(
        "api/upload_goal_song/",
        UploadGoalSongAPIView.as_view(),
        name="player-upload-goal-song",
    ),
    path(
        "spotify/connect/",
        SpotifyConnectAPIView.as_view(),
        name="player-spotify-connect",
    ),
    path(
        "spotify/callback/",
        SpotifyCallbackView.as_view(),
        name="player-spotify-callback",
    ),
    path(
        "spotify/play/",
        SpotifyPlayAPIView.as_view(),
        name="player-spotify-play",
    ),
    path(
        "me/connected-clubs/recent-results/",
        PlayerConnectedClubRecentResultsAPIView.as_view(),
        name="player-connected-clubs-recent-results",
    ),
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
