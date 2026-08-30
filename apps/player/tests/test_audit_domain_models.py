# ruff: noqa: D103
"""Audit tests for player-domain model and privacy invariants."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.tests.tracker_test_helpers import create_tracker_player
from apps.player.models import CachedSong, PlayerSong
from apps.player.models.cached_song import CachedSongStatus
from apps.player.models.player import Player
from apps.player.models.player_club_membership import PlayerClubMembership
from apps.player.models.player_song import PlayerSongStatus
from apps.player.models.spotify_token import SpotifyToken
from apps.player.privacy import can_view_by_visibility


EXPECTED_HISTORY_ROWS = 3
EXPECTED_PLAYER_SONG_ROWS = 3
CACHED_DURATION_SECONDS = 123
DIRECT_DURATION_SECONDS = 789


@pytest.mark.django_db
def test_active_member_clubs_respects_inclusive_date_window() -> None:
    on = date(2026, 1, 15)
    player = create_tracker_player(username="membership-window")
    clubs = {
        name: Club.objects.create(name=f"Membership {name}")
        for name in ("expired", "starts", "ends", "future", "open")
    }
    PlayerClubMembership.objects.bulk_create([
        PlayerClubMembership(
            player=player,
            club=clubs["expired"],
            start_date=on - timedelta(days=10),
            end_date=on - timedelta(days=1),
        ),
        PlayerClubMembership(
            player=player,
            club=clubs["starts"],
            start_date=on,
        ),
        PlayerClubMembership(
            player=player,
            club=clubs["ends"],
            start_date=on - timedelta(days=10),
            end_date=on,
        ),
        PlayerClubMembership(
            player=player,
            club=clubs["future"],
            start_date=on + timedelta(days=1),
        ),
        PlayerClubMembership(
            player=player,
            club=clubs["open"],
            start_date=on - timedelta(days=10),
        ),
    ])

    assert set(player.active_member_clubs(on=on)) == {
        clubs["starts"],
        clubs["ends"],
        clubs["open"],
    }


@pytest.mark.django_db
def test_player_club_membership_enforces_history_constraints() -> None:
    player = create_tracker_player(username="membership-constraints")
    club = Club.objects.create(name="Membership Constraints")
    start = date(2026, 2, 1)

    with pytest.raises(IntegrityError), transaction.atomic():
        PlayerClubMembership.objects.create(
            player=player,
            club=club,
            start_date=start,
            end_date=start - timedelta(days=1),
        )

    PlayerClubMembership.objects.create(
        player=player,
        club=club,
        start_date=start,
        end_date=start,
    )
    PlayerClubMembership.objects.create(
        player=player,
        club=club,
        start_date=start + timedelta(days=1),
        end_date=start + timedelta(days=2),
    )
    PlayerClubMembership.objects.create(
        player=player,
        club=club,
        start_date=start + timedelta(days=3),
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        PlayerClubMembership.objects.create(
            player=player,
            club=club,
            start_date=start + timedelta(days=4),
        )

    assert (
        PlayerClubMembership.objects.filter(player=player, club=club).count()
        == EXPECTED_HISTORY_ROWS
    )


@pytest.mark.django_db
def test_player_song_cached_source_constraint_is_conditional() -> None:
    first = create_tracker_player(username="cached-constraint-first")
    second = create_tracker_player(username="cached-constraint-second")
    cached = CachedSong.objects.create(
        spotify_url="https://open.spotify.com/track/cached-constraint"
    )
    PlayerSong.objects.create(player=first, cached_song=cached)
    PlayerSong.objects.create(player=second, cached_song=cached)

    with pytest.raises(IntegrityError), transaction.atomic():
        PlayerSong.objects.create(player=first, cached_song=cached)

    PlayerSong.objects.create(player=first)
    PlayerSong.objects.create(player=first)

    assert PlayerSong.objects.filter(player=first).count() == EXPECTED_PLAYER_SONG_ROWS


@pytest.mark.django_db
def test_player_song_effective_fields_delegate_to_cached_source() -> None:
    player = create_tracker_player(username="effective-song")
    cached = CachedSong.objects.create(
        spotify_url="https://open.spotify.com/track/effective-song",
        title="Cached title",
        artists="Cached artists",
        duration_seconds=CACHED_DURATION_SECONDS,
        status=CachedSongStatus.READY,
        error_message="cached error",
        audio_file="cached_songs/cached.mp3",
    )
    linked = PlayerSong.objects.create(
        player=player,
        cached_song=cached,
        title="Local title",
        artists="Local artists",
        duration_seconds=456,
        status=PlayerSongStatus.FAILED,
        error_message="local error",
        audio_file="player_songs/local.mp3",
    )
    direct = PlayerSong.objects.create(
        player=player,
        title="Direct title",
        artists="Direct artists",
        duration_seconds=DIRECT_DURATION_SECONDS,
        status=PlayerSongStatus.UPLOADING,
        error_message="direct error",
        audio_file="player_songs/direct.mp3",
    )

    assert linked.effective_audio_file.name == "cached_songs/cached.mp3"
    assert linked.effective_status == CachedSongStatus.READY
    assert linked.effective_error_message == "cached error"
    assert linked.effective_title == "Cached title"
    assert linked.effective_artists == "Cached artists"
    assert linked.effective_duration_seconds == CACHED_DURATION_SECONDS
    assert linked.effective_updated_at == cached.updated_at

    assert direct.effective_audio_file.name == "player_songs/direct.mp3"
    assert direct.effective_status == PlayerSongStatus.UPLOADING
    assert direct.effective_error_message == "direct error"
    assert direct.effective_title == "Direct title"
    assert direct.effective_artists == "Direct artists"
    assert direct.effective_duration_seconds == DIRECT_DURATION_SECONDS
    assert direct.effective_updated_at == direct.updated_at


@pytest.mark.django_db
def test_player_visibility_policy_is_owner_first_and_fails_closed() -> None:
    owner = create_tracker_player(username="privacy-owner")
    viewer = create_tracker_player(username="privacy-viewer")

    assert can_view_by_visibility(visibility="unknown", viewer=owner, target=owner)
    assert can_view_by_visibility(
        visibility=Player.Visibility.PUBLIC,
        viewer=None,
        target=owner,
    )
    assert not can_view_by_visibility(
        visibility=Player.Visibility.CLUB,
        viewer=None,
        target=owner,
    )
    assert not can_view_by_visibility(visibility="unknown", viewer=viewer, target=owner)

    with patch(
        "apps.player.privacy.viewer_connected_to_player_club",
        return_value=True,
    ) as connected:
        assert can_view_by_visibility(
            visibility=Player.Visibility.PRIVATE,
            viewer=viewer,
            target=owner,
        )

    connected.assert_called_once_with(viewer=viewer, target=owner)


@pytest.mark.django_db
def test_user_signal_creates_player_once_and_ignores_updates() -> None:
    player = create_tracker_player(username="signal-player")
    user = player.user

    user.email = "updated@example.invalid"
    user.save(update_fields=["email"])

    assert Player.objects.filter(user=user).count() == 1
    assert Player.objects.get(user=user).id_uuid == player.id_uuid


@pytest.mark.django_db
def test_spotify_token_expiration_is_strictly_after_deadline() -> None:
    player = create_tracker_player(username="spotify-expiration")
    deadline = timezone.now()
    token = SpotifyToken(
        user=player.user,
        access_token="access",
        refresh_token="refresh",
        spotify_user_id="spotify-expiration",
        expires_at=deadline,
    )

    with patch("apps.player.models.spotify_token.now", return_value=deadline):
        assert token.is_token_expired() is False
    with patch(
        "apps.player.models.spotify_token.now",
        return_value=deadline + timedelta(microseconds=1),
    ):
        assert token.is_token_expired() is True
