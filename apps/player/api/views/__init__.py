"""Player API views, split by domain responsibility."""

from .goal_song import CurrentPlayerGoalSongAPIView
from .overview import (
    PlayerConnectedClubRecentResultsAPIView,
    PlayerOverviewAPIView,
    PlayerStatsAPIView,
)
from .player_profile import (
    CurrentPlayerAPIView,
    CurrentPlayerFollowedTeamsAPIView,
    CurrentPlayerPrivacySettingsAPIView,
    CurrentPlayerTeamsAPIView,
    PlayerFollowedTeamsAPIView,
    PlayerTeamsAPIView,
    PlayerViewSet,
)
from .push import (
    CurrentPlayerPushSubscriptionsAPIView,
    CurrentPlayerTestPushNotificationAPIView,
)
from .songs import (
    CurrentPlayerSongDetailAPIView,
    CurrentPlayerSongRetryAPIView,
    CurrentPlayerSongsAPIView,
    PlayerSongClipAPIView,
)
from .spotify import (
    SpotifyCallbackView,
    SpotifyConnectAPIView,
    SpotifyPauseAPIView,
    SpotifyPlayAPIView,
)
from .uploads import UploadGoalSongAPIView, UploadProfilePictureAPIView


__all__ = [
    "CurrentPlayerAPIView",
    "CurrentPlayerFollowedTeamsAPIView",
    "CurrentPlayerGoalSongAPIView",
    "CurrentPlayerPrivacySettingsAPIView",
    "CurrentPlayerPushSubscriptionsAPIView",
    "CurrentPlayerSongDetailAPIView",
    "CurrentPlayerSongRetryAPIView",
    "CurrentPlayerSongsAPIView",
    "CurrentPlayerTeamsAPIView",
    "CurrentPlayerTestPushNotificationAPIView",
    "PlayerConnectedClubRecentResultsAPIView",
    "PlayerFollowedTeamsAPIView",
    "PlayerOverviewAPIView",
    "PlayerSongClipAPIView",
    "PlayerStatsAPIView",
    "PlayerTeamsAPIView",
    "PlayerViewSet",
    "SpotifyCallbackView",
    "SpotifyConnectAPIView",
    "SpotifyPauseAPIView",
    "SpotifyPlayAPIView",
    "UploadGoalSongAPIView",
    "UploadProfilePictureAPIView",
]
