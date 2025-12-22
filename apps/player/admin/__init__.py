"""Package contains the admin classes for the player app."""

from .cached_song_admin import CachedSongAdmin
from .player_admin import PlayerAdmin
from .player_club_membership_admin import PlayerClubMembershipAdmin
from .player_song_admin import PlayerSongAdmin
from .push_subscription_admin import PlayerPushSubscriptionAdmin
from .spotify_token_admin import SpotifyTokenAdmin


__all__ = [
    "CachedSongAdmin",
    "PlayerAdmin",
    "PlayerClubMembershipAdmin",
    "PlayerPushSubscriptionAdmin",
    "PlayerSongAdmin",
    "SpotifyTokenAdmin",
]
