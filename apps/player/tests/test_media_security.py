"""Verify object isolation, uniform upload limits, and expiring media access."""

from __future__ import annotations

from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from django.contrib.auth.models import User
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse
from django.test import Client
from django.utils import timezone
from PIL import Image
import pytest
from pytest_django.fixtures import Settings
from storages.backends.s3 import S3Storage

from apps.player.adapters.outbound.private_storage import (
    MEDIA_URL_REUSE_SECONDS,
    PrivateMediaStorage,
)
from apps.player.composition import song_jobs
from apps.player.media_paths import (
    delete_with_variant,
    player_picture_path,
    player_song_path,
    variant_key,
    variant_url,
)
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


@pytest.mark.parametrize(
    ("content", "accepted"),
    [
        (b"ID3\x04\x00synthetic", True),
        (b"\xff\xfb\x90\x64synthetic", True),
        (b"#EXTM3U\n#EXT-X-TARGETDURATION:1\nfile:///etc/passwd\n", False),
        (b"<html>synthetic</html>", False),
    ],
)
def test_audio_uploads_are_checked_by_content(
    client: Client, content: bytes, accepted: bool
) -> None:
    """An .mp3 name and audio MIME type cannot smuggle a playlist to ffmpeg."""
    user = User.objects.create_user(username="sniffed-upload")
    client.force_login(user)
    with patch("apps.player.composition.song_jobs.player_song"):
        response = client.post(
            "/api/player/me/songs/",
            {
                "audio_file": SimpleUploadedFile(
                    "track.mp3", content, content_type="audio/mpeg"
                )
            },
        )
    assert (response.status_code < HTTPStatus.BAD_REQUEST) is accepted
    assert PlayerSong.objects.filter(player=user.player).exists() is accepted


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
        cache_control, max_age = response["Cache-Control"].split("=")
        assert cache_control == "private, max-age"
        assert (
            settings.KORFBAL_MEDIA_URL_MAX_AGE - MEDIA_URL_REUSE_SECONDS
            <= int(max_age)
            <= settings.KORFBAL_MEDIA_URL_MAX_AGE
        )
        assert response["Content-Security-Policy"].startswith("sandbox")
        assert client.get(path + "tampered").status_code == HTTPStatus.FORBIDDEN
        now = timezone.now().timestamp()
        with patch(
            "django.core.signing.time.time",
            return_value=now + settings.KORFBAL_MEDIA_URL_MAX_AGE + 1,
        ):
            assert client.get(path).status_code == HTTPStatus.FORBIDDEN
        opened.assert_called_once()


def test_media_url_is_reused_within_a_window_without_outliving_the_limit(
    client: Client, settings: Settings
) -> None:
    """Repeated reads share a URL; it still expires within the configured age."""
    storage = PrivateMediaStorage(
        access_key="synthetic", secret_key="synthetic", bucket_name="test"
    )
    key = "profile_pictures/owner/avatar.png"
    start = 1_800_000_000 - 1_800_000_000 % MEDIA_URL_REUSE_SECONDS
    clock = "django.core.signing.time.time"
    with patch(clock, return_value=start + 1):
        first = storage.url(key)
        assert storage.url("profile_pictures/other/avatar.png") != first
    with patch(clock, return_value=start + MEDIA_URL_REUSE_SECONDS - 1):
        late = storage.url(key)
    with patch(clock, return_value=start + MEDIA_URL_REUSE_SECONDS):
        following = storage.url(key)
    assert late == first
    assert following != first
    assert MEDIA_URL_REUSE_SECONDS < settings.KORFBAL_MEDIA_URL_MAX_AGE

    url = urlsplit(late)
    path = f"{url.path}?{url.query}"
    with patch(
        "apps.player.api.views.media.audio_storage.open",
        return_value=BytesIO(b"synthetic"),
    ):
        # Browsers may keep the object only for the rest of the URL's lifetime.
        with patch(clock, return_value=start + settings.KORFBAL_MEDIA_URL_MAX_AGE - 90):
            response = client.get(path)
            assert response["Cache-Control"] == "private, max-age=90"
        with patch(clock, return_value=start + settings.KORFBAL_MEDIA_URL_MAX_AGE):
            response = client.get(path)
            assert response.status_code == HTTPStatus.OK
            assert response["Cache-Control"] == "private, max-age=0"
        with patch(clock, return_value=start + settings.KORFBAL_MEDIA_URL_MAX_AGE + 1):
            assert client.get(path).status_code == HTTPStatus.FORBIDDEN


def _jpeg() -> bytes:
    encoded = BytesIO()
    Image.new("RGB", (900, 1200), (20, 120, 200)).save(encoded, "JPEG")
    return encoded.getvalue()


def test_profile_pictures_are_served_as_small_variants(
    client: Client, settings: Settings, tmp_path: Path
) -> None:
    """Pictures resize on first use; other private media never does."""
    settings.MEDIA_ROOT = tmp_path
    owner = User.objects.create_user(username="variant-owner").player
    owner.profile_picture = default_storage.save(
        player_picture_path(owner, "avatar.jpg"), BytesIO(_jpeg())
    )
    owner.save(update_fields=["profile_picture"])
    song = default_storage.save("player_songs/owner/track.mp3", BytesIO(b"ID3"))
    storage = PrivateMediaStorage(
        access_key="synthetic", secret_key="synthetic", bucket_name="test"
    )

    def fetch(key: str) -> HttpResponse:
        url = urlsplit(variant_url(storage.url(key)))
        return client.get(f"{url.path}?{url.query}")

    assert owner.get_profile_picture().endswith("?variant=w256")
    response = fetch(owner.profile_picture.name)
    assert response.status_code == HTTPStatus.OK
    assert response["Content-Type"] == "image/webp"
    assert response["Cache-Control"].startswith("private, max-age=")
    with Image.open(BytesIO(response.content)) as image:
        assert image.size == (192, 256)
    assert default_storage.exists(variant_key(owner.profile_picture.name))
    assert b"".join(fetch(song).streaming_content) == b"ID3"
    assert not default_storage.exists(variant_key(song))

    delete_with_variant(default_storage, owner.profile_picture.name)
    assert not default_storage.exists(owner.profile_picture.name)
    assert not default_storage.exists(variant_key(owner.profile_picture.name))
