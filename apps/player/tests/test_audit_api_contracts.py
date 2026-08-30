"""Audit regressions for player API boundary contracts."""

from __future__ import annotations

from http import HTTPStatus
import json
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
import pytest

from apps.player.models.player import Player
from apps.player.models.player_song import PlayerSong, PlayerSongStatus
from apps.player.models.push_subscription import PlayerPushSubscription
from apps.player.services.player_songs import PlayerSongClipRequest


ORIGINAL_START_SECONDS = 4
MISSING_SONG_ID = "00000000-0000-0000-0000-000000000001"


def _player_for(user: User) -> Player:
    """Return the signal-created player with a strict static type."""
    return Player.objects.get(user=user)


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_public_player_list_minimises_personal_data_and_keeps_list_shape(
    client: Client,
) -> None:
    """Anonymous list responses must not expose private Django user fields."""
    user = User.objects.create_user(
        username="public-list-player",
        email="private@example.test",
        first_name="Private",
        last_name="Person",
    )

    response = client.get("/api/player/players/")

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert isinstance(payload, list)
    serialized_player = next(
        row for row in payload if row["id_uuid"] == str(_player_for(user).id_uuid)
    )
    assert serialized_player["user"] == {
        "id": user.pk,
        "username": "public-list-player",
    }


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_detail_includes_personal_data_only_for_owner(client: Client) -> None:
    """The same profile representation changes safely with viewer identity."""
    user = User.objects.create_user(
        username="private-detail-player",
        email="owner@example.test",
        first_name="Owner",
        last_name="Only",
    )
    path = f"/api/player/players/{_player_for(user).id_uuid}/"

    anonymous_response = client.get(path)
    assert anonymous_response.status_code == HTTPStatus.OK
    assert anonymous_response.json()["user"] == {
        "id": user.pk,
        "username": "private-detail-player",
    }

    client.force_login(user)
    owner_response = client.get(path)
    assert owner_response.status_code == HTTPStatus.OK
    assert owner_response.json()["user"] == {
        "id": user.pk,
        "username": "private-detail-player",
        "email": "owner@example.test",
        "first_name": "Owner",
        "last_name": "Only",
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/player/me/password/", "post"),
        ("/api/player/me/privacy-settings/", "get"),
        ("/api/player/me/songs/", "get"),
        (f"/api/player/me/songs/{MISSING_SONG_ID}/", "patch"),
        (f"/api/player/me/songs/{MISSING_SONG_ID}/", "delete"),
        (f"/api/player/me/songs/{MISSING_SONG_ID}/retry/", "post"),
        ("/api/player/spotify/callback/", "get"),
    ],
)
@override_settings(SECURE_SSL_REDIRECT=False)
def test_protected_player_routes_return_json_auth_errors_without_redirects(
    client: Client,
    path: str,
    method: str,
) -> None:
    """Protected API routes must not redirect anonymous clients to HTML login."""
    request = getattr(client, method)
    response = request(path, content_type="application/json")

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.headers["Content-Type"].startswith("application/json")
    assert "Location" not in response.headers


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_account_patch_rejects_duplicate_username_without_partial_update(
    client: Client,
) -> None:
    """Account validation errors must leave both mutable user fields unchanged."""
    User.objects.create_user(username="already-taken")
    user = User.objects.create_user(
        username="account-original",
        email="original@example.test",
    )
    client.force_login(user)

    response = client.patch(
        "/api/player/me/",
        data=json.dumps({
            "username": "  already-taken  ",
            "email": "changed@example.test",
        }),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json() == {"username": ["This username is already in use."]}
    user.refresh_from_db()
    assert user.username == "account-original"
    assert user.email == "original@example.test"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("payload", "error_field"),
    [
        ({}, "non_field_errors"),
        ({"stats_visibility": "friends"}, "stats_visibility"),
    ],
)
@override_settings(SECURE_SSL_REDIRECT=False)
def test_privacy_patch_rejects_empty_or_unknown_settings(
    client: Client,
    payload: dict[str, object],
    error_field: str,
) -> None:
    """Privacy writes accept neither no-op payloads nor unknown visibility values."""
    user = User.objects.create_user(username=f"privacy-{error_field}")
    client.force_login(user)

    response = client.patch(
        "/api/player/me/privacy-settings/",
        data=json.dumps(payload),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert error_field in response.json()
    player = _player_for(user)
    assert player.stats_visibility == Player.Visibility.PUBLIC


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("uploaded", "expected_error"),
    [
        (
            SimpleUploadedFile("song.wav", b"audio", content_type="audio/mpeg"),
            "Only MP3 uploads are supported.",
        ),
        (
            SimpleUploadedFile("song.mp3", b"audio", content_type="audio/wav"),
            "Invalid content type (expected MP3).",
        ),
    ],
)
@override_settings(SECURE_SSL_REDIRECT=False)
def test_song_create_rejects_invalid_upload_metadata(
    client: Client,
    uploaded: SimpleUploadedFile,
    expected_error: str,
) -> None:
    """Upload validation rejects misleading extensions and media types."""
    user = User.objects.create_user(username=f"upload-{uploaded.name}")
    client.force_login(user)

    response = client.post("/api/player/me/songs/", data={"audio_file": uploaded})

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["audio_file"] == [expected_error]
    assert not PlayerSong.objects.filter(player=_player_for(user)).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"start_time_seconds": -1},
        {"playback_speed": 0.49},
        {"playback_speed": 2.01},
    ],
)
@override_settings(SECURE_SSL_REDIRECT=False)
def test_song_patch_rejects_noop_and_out_of_range_settings(
    client: Client,
    payload: dict[str, Any],
) -> None:
    """Song settings enforce their complete validation boundary before mutation."""
    user = User.objects.create_user(username=f"song-patch-{uuid4()}")
    song = PlayerSong.objects.create(
        player=_player_for(user),
        start_time_seconds=ORIGINAL_START_SECONDS,
        playback_speed=1.0,
    )
    client.force_login(user)

    response = client.patch(
        f"/api/player/me/songs/{song.id_uuid}/",
        data=json.dumps(payload),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    song.refresh_from_db()
    assert song.start_time_seconds == ORIGINAL_START_SECONDS
    assert song.playback_speed == pytest.approx(1.0)


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_song_retry_hides_other_users_song_and_rejects_ready_song(
    client: Client,
) -> None:
    """Retry preserves ownership hiding and the already-ready error contract."""
    owner = User.objects.create_user(username="retry-owner")
    other = User.objects.create_user(username="retry-other")
    song = PlayerSong.objects.create(
        player=_player_for(owner),
        status=PlayerSongStatus.READY,
    )

    client.force_login(other)
    hidden_response = client.post(f"/api/player/me/songs/{song.id_uuid}/retry/")
    assert hidden_response.status_code == HTTPStatus.NOT_FOUND
    assert hidden_response.json() == {"detail": "Song not found"}

    client.force_login(owner)
    ready_response = client.post(f"/api/player/me/songs/{song.id_uuid}/retry/")
    assert ready_response.status_code == HTTPStatus.BAD_REQUEST
    assert ready_response.json() == {"detail": "Song is already ready"}


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_push_subscription_delete_cannot_target_another_user_by_id(
    client: Client,
) -> None:
    """Subscription identifiers remain scoped to the authenticated owner."""
    owner = User.objects.create_user(username="push-delete-owner")
    other = User.objects.create_user(username="push-delete-other")
    subscription = PlayerPushSubscription.objects.create(
        user=owner,
        endpoint="https://example.test/push/owner",
        subscription={
            "endpoint": "https://example.test/push/owner",
            "keys": {"p256dh": "key", "auth": "secret"},
        },
    )
    client.force_login(other)

    response = client.delete(
        "/api/player/me/push-subscriptions/",
        data=json.dumps({"id_uuid": str(subscription.id_uuid)}),
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json() == {"detail": "Subscription not found"}
    subscription.refresh_from_db()
    assert subscription.is_active is True


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("query", "expected_limit", "expected_days"),
    [
        ({"limit": "0", "days": "0"}, 1, None),
        ({"limit": "999", "days": "14"}, 10, 14),
        ({"limit": "invalid", "days": "invalid"}, 3, None),
    ],
)
@override_settings(SECURE_SSL_REDIRECT=False)
def test_connected_results_normalises_query_parameters(
    client: Client,
    query: dict[str, str],
    expected_limit: int,
    expected_days: int | None,
) -> None:
    """Recent-results filters clamp limits and discard invalid day windows."""
    user = User.objects.create_user(username=f"results-{uuid4()}")
    client.force_login(user)

    with patch(
        "apps.player.api.views.overview.connected_club_recent_results",
        return_value=[],
    ) as recent_results:
        response = client.get(
            "/api/player/me/connected-clubs/recent-results/",
            data={**query, "season": "season-id"},
        )

    assert response.status_code == HTTPStatus.OK
    assert response.json() == []
    recent_results.assert_called_once_with(
        player=recent_results.call_args.kwargs["player"],
        limit=expected_limit,
        days=expected_days,
        season_id="season-id",
    )
    assert (
        recent_results.call_args.kwargs["player"].id_uuid == _player_for(user).id_uuid
    )


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_song_clip_normalises_query_parameters_before_resolution(
    client: Client,
) -> None:
    """Clip query coercion clamps unsafe values before application dispatch."""
    song_id = uuid4()
    with patch(
        "apps.player.api.views.songs.resolve_player_song_clip",
        return_value=None,
    ) as resolve_clip:
        response = client.get(
            f"/api/player/api/songs/{song_id}/clip/",
            data={"start": "-5.9", "duration": "100.7", "stream": "1"},
        )

    assert response.status_code == HTTPStatus.FOUND
    assert response["Location"] == "/"
    resolve_clip.assert_called_once_with(
        request=PlayerSongClipRequest(
            song_id=cast(str, song_id),
            start_seconds=0,
            duration_seconds=15,
            enqueue_if_missing=True,
        )
    )
