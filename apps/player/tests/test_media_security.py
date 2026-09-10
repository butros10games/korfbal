"""Verify object isolation, uniform upload limits, and expiring media access."""

from __future__ import annotations

from http import HTTPStatus
from io import BytesIO
from unittest.mock import patch
from urllib.parse import urlsplit

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone
import pytest
from pytest_django.fixtures import Settings
from storages.backends.s3 import S3Storage

from apps.player.adapters.outbound.private_storage import PrivateMediaStorage
from apps.player.composition import song_jobs
from apps.player.media_paths import player_picture_path, player_song_path
from apps.player.models import PlayerSong
from apps.player.services.player_songs import create_player_song
from apps.player.services.upload_validation import InvalidAudioUploadError


pytestmark = pytest.mark.django_db


def test_s3_names_never_collide_between_users_or_uploads() -> None:
    """Check generated names with S3's overwrite-prone naming behavior."""
    first = User.objects.create_user(username="first-upload").player
    second = User.objects.create_user(username="second-upload").player
    storage = S3Storage(
        access_key="synthetic", secret_key="synthetic", bucket_name="test"
    )
    names = [player_picture_path(p, "avatar.png") for p in (first, second, first)]
    songs = [
        player_song_path(PlayerSong(player=p), "track.mp3")
        for p in (first, second, first)
    ]
    assert len({storage.get_available_name(name) for name in names + songs}) == len(
        names + songs
    )


@pytest.mark.parametrize(
    ("route", "field"),
    [
        ("/api/player/me/songs/", "audio_file"),
        ("/api/player/api/upload_goal_song/", "goal_song"),
    ],
)
def test_every_audio_route_rejects_active_extension(
    client: Client, route: str, field: str
) -> None:
    """A forged MIME type cannot admit HTML through the legacy route."""
    user = User.objects.create_user(username="html-upload")
    client.force_login(user)
    response = client.post(
        route,
        {
            field: SimpleUploadedFile(
                "payload.html", b"synthetic", content_type="audio/mpeg"
            )
        },
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert not PlayerSong.objects.filter(player=user.player).exists()


def test_creation_service_enforces_size_before_storage_or_dispatch() -> None:
    """Calling the shared command directly cannot bypass route validation."""
    user = User.objects.create_user(username="large-upload")
    uploaded = SimpleUploadedFile("track.mp3", b"ID3", content_type="audio/mpeg")
    uploaded.size = 26 * 1024 * 1024
    with patch("apps.player.composition.song_jobs.player_song") as dispatch:
        with pytest.raises(InvalidAudioUploadError):
            create_player_song(
                player=user.player,
                uploaded_audio=uploaded,
                spotify_url=None,
                jobs=song_jobs,
            )
        dispatch.assert_not_called()
    assert not PlayerSong.objects.exists()


def test_media_capability_expires_and_never_redirects_to_bucket(
    client: Client, settings: Settings
) -> None:
    """Tampered and expired URLs cannot reach the storage adapter."""
    storage = PrivateMediaStorage(
        access_key="synthetic", secret_key="synthetic", bucket_name="test"
    )
    url = urlsplit(storage.url("profile_pictures/owner/avatar.png"))
    path = f"{url.path}?{url.query}"
    with patch(
        "apps.player.api.views.media.audio_storage.open",
        return_value=BytesIO(b"synthetic"),
    ) as opened:
        response = client.get(path)
        assert response.status_code == HTTPStatus.OK
        assert b"".join(response.streaming_content) == b"synthetic"
        assert response["Cache-Control"] == "private, no-store"
        assert response["Content-Security-Policy"].startswith("sandbox")
        assert client.get(path + "tampered").status_code == HTTPStatus.FORBIDDEN
        now = timezone.now().timestamp()
        with patch(
            "django.core.signing.time.time",
            return_value=now + settings.KORFBAL_MEDIA_URL_MAX_AGE + 1,
        ):
            assert client.get(path).status_code == HTTPStatus.FORBIDDEN
        opened.assert_called_once()
