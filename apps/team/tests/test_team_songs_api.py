"""Team-owned audio imports, management boundaries, and tracker fallback contracts."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
import pytest

from apps.kwt_common.models import BackgroundJob
from apps.player.models import PlayerSong, PlayerSongStatus
from apps.player.services.goal_song_manifest import build_goal_song_manifest
from apps.team.models import TeamData
from apps.team.tests.team_test_support import (
    TeamTestContext,
    build_team_context,
    create_season,
    create_song,
)


pytestmark = pytest.mark.django_db
START_SECONDS = 42
PERSONAL_START_SECONDS = 7
UPLOAD_COUNT = 2
SOURCE = f"https://youtu.be/BaW_jenozKc?t={START_SECONDS}"


@pytest.fixture
def team_context(client: Client) -> TeamTestContext:
    """Authorize a coach who is not a roster player."""
    context = build_team_context(suffix="team-audio")
    assert context.coach.user is not None
    client.force_login(context.coach.user)
    return context


def _path(context: TeamTestContext, suffix: str = "songs/") -> str:
    return (
        f"/api/team/teams/{context.team.pk}/goal-song-admin/{suffix}"
        f"?season={context.season.pk}"
    )


def _import(client: Client, context: TeamTestContext) -> PlayerSong:
    response = client.post(
        _path(context), {"source_url": SOURCE}, content_type="application/json"
    )
    assert response.status_code == HTTPStatus.CREATED
    return PlayerSong.objects.select_related("cached_song").get(
        pk=response.json()["id_uuid"]
    )


def _manifest(context: TeamTestContext) -> dict[str, Any]:
    return build_goal_song_manifest(
        player_ids=[str(context.player.pk)], team=context.team, season=context.season
    )


def test_team_import_is_owned_by_team_idempotent_and_prepared_in_worker(
    client: Client, team_context: TeamTestContext
) -> None:
    """An imported team song never becomes the coach's personal song."""
    song = _import(client, team_context)
    assert song.player_id is None
    assert song.team_data_id == team_context.team_data.pk
    assert song.start_time_seconds == START_SECONDS
    assert song.spotify_url == "https://www.youtube.com/watch?v=BaW_jenozKc"
    assert BackgroundJob.objects.filter(
        task="apps.player.tasks.download_player_song", args=[str(song.pk)]
    ).exists()
    assert client.get("/api/player/me/songs/").json() == []
    repeat = client.post(
        _path(team_context),
        {"source_url": "https://www.youtube.com/watch?v=BaW_jenozKc&t=9"},
        content_type="application/json",
    )
    assert repeat.status_code == HTTPStatus.OK
    assert repeat.json()["id_uuid"] == str(song.pk)
    assert repeat.json()["start_time_seconds"] == START_SECONDS
    team_context.team_data.refresh_from_db()
    assert team_context.team_data.fallback_goal_song_song_ids == [str(song.pk)]
    assert _manifest(team_context)["fallback"] == []

    cached = song.cached_song
    assert cached is not None
    cached.status = PlayerSongStatus.READY
    cached.audio_file.save("synthetic.mp3", ContentFile(b"ID3"), save=True)
    manifest = _manifest(team_context)
    assert manifest["players"] == {}
    assert [entry["id"] for entry in manifest["fallback"]] == [str(song.pk)]
    assert "start=42" in manifest["fallback"][0]["url"]
    admin = client.get(_path(team_context, "")).json()
    assert admin["team_songs"][0]["id_uuid"] == str(song.pk)
    assert "player_id" not in admin["fallback_goal_song_songs"][0]


def test_team_upload_uses_immutable_team_keys_and_is_selected(
    client: Client, team_context: TeamTestContext
) -> None:
    """Identical upload names do not overwrite team or player media."""
    paths = []
    for _ in range(UPLOAD_COUNT):
        response = client.post(
            _path(team_context),
            {
                "audio_file": SimpleUploadedFile(
                    "goal.mp3", b"ID3synthetic", content_type="audio/mpeg"
                )
            },
        )
        assert response.status_code == HTTPStatus.CREATED
        song = PlayerSong.objects.get(pk=response.json()["id_uuid"])
        assert song.player_id is None
        assert song.title == "goal"
        paths.append(song.audio_file.name)
        assert song.audio_file.name.startswith(
            f"team_songs/{team_context.team_data.pk}/"
        )
        assert BackgroundJob.objects.filter(
            task="apps.player.tasks.download_player_song", args=[str(song.pk)]
        ).exists()
    assert len(set(paths)) == UPLOAD_COUNT
    assert len(_manifest(team_context)["fallback"]) == UPLOAD_COUNT


@pytest.mark.parametrize(
    "payload",
    [
        {"source_url": "https://youtube.com/playlist?list=invalid"},
        {"source_url": "https://example.com/audio.mp3"},
        {},
    ],
)
def test_team_import_rejects_invalid_sources_before_writing(
    client: Client, team_context: TeamTestContext, payload: dict[str, str]
) -> None:
    """Alternate team routes retain source validation and produce no jobs."""
    before = BackgroundJob.objects.count()
    response = client.post(
        _path(team_context), payload, content_type="application/json"
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert not PlayerSong.objects.filter(team_data=team_context.team_data).exists()
    assert BackgroundJob.objects.count() == before


def test_team_upload_retains_shared_file_validation(
    client: Client, team_context: TeamTestContext
) -> None:
    """The team endpoint cannot bypass the shared upload policy."""
    response = client.post(
        _path(team_context),
        {
            "audio_file": SimpleUploadedFile(
                "audio.html", b"<html>bad</html>", content_type="text/html"
            )
        },
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert not PlayerSong.objects.exists()


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("post", "songs/"),
        ("patch", "songs/{song}/"),
        ("delete", "songs/{song}/"),
        ("post", "songs/{song}/retry/"),
    ],
)
@pytest.mark.parametrize("authenticated", [False, True])
def test_team_library_writes_require_management_rights(
    client: Client,
    team_context: TeamTestContext,
    method: str,
    suffix: str,
    authenticated: bool,
) -> None:
    """Anonymous viewers and ordinary roster players cannot mutate team audio."""
    song = _import(client, team_context)
    client.logout()
    if authenticated:
        assert team_context.player.user is not None
        client.force_login(team_context.player.user)
    response = getattr(client, method)(
        _path(team_context, suffix.format(song=song.pk)),
        {"source_url": SOURCE},
        content_type="application/json",
    )
    assert response.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}
    assert response.headers["Content-Type"].startswith("application/json")
    assert PlayerSong.objects.filter(pk=song.pk).exists()


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("patch", "songs/{song}/"),
        ("delete", "songs/{song}/"),
        ("post", "songs/{song}/retry/"),
    ],
)
def test_team_song_writes_reject_other_owners_and_seasons(
    client: Client, team_context: TeamTestContext, method: str, suffix: str
) -> None:
    """UUID knowledge does not grant access across team, season or personal owners."""
    other = build_team_context(suffix="other-audio")
    old_season = create_season("Old team audio", starts_in_days=-700, ends_in_days=-365)
    old_team_data = TeamData.objects.create(team=team_context.team, season=old_season)
    foreign = [
        PlayerSong.objects.create(team_data=other.team_data),
        PlayerSong.objects.create(team_data=old_team_data),
        create_song(player=team_context.player, title="Personal"),
    ]
    for song in foreign:
        response = getattr(client, method)(
            _path(team_context, suffix.format(song=song.pk)),
            {"start_time_seconds": 15},
            content_type="application/json",
        )
        assert response.status_code == HTTPStatus.NOT_FOUND
        assert PlayerSong.objects.filter(pk=song.pk).exists()


def test_team_playlist_supports_pending_imports_and_personal_priority(
    client: Client, team_context: TeamTestContext
) -> None:
    """Pending imports can be selected without changing personal choices."""
    team_song = _import(client, team_context)
    personal = create_song(player=team_context.player, title="Personal")
    team_context.player.goal_song_song_ids = [str(personal.pk)]
    team_context.player.save(update_fields=["goal_song_song_ids"])
    ids = [str(team_song.pk), str(personal.pk)]
    response = client.patch(
        _path(team_context, "fallback/"),
        {"fallback_goal_song_song_ids": ids},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.OK
    manifest = _manifest(team_context)
    assert [
        entry["id"] for entry in manifest["players"][str(team_context.player.pk)]
    ] == [str(personal.pk)]
    assert [entry["id"] for entry in manifest["fallback"]] == [str(personal.pk)]
    client.patch(
        _path(team_context, f"player/{team_context.player.pk}/"),
        {"goal_song_song_ids": []},
        content_type="application/json",
    )
    assert _manifest(team_context)["players"] == {}
    assert _manifest(team_context)["fallback"]
    other = build_team_context(suffix="forbidden-fallback")
    foreign = PlayerSong.objects.create(team_data=other.team_data)
    response = client.patch(
        _path(team_context, "fallback/"),
        {"fallback_goal_song_song_ids": [str(foreign.pk)]},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST


def test_team_song_settings_retry_and_delete_keep_personal_audio(
    client: Client, team_context: TeamTestContext
) -> None:
    """The team owns its clip settings, lifecycle, and selection cleanup."""
    song = _import(client, team_context)
    personal = create_song(
        player=team_context.player,
        title="Personal",
        start_time_seconds=PERSONAL_START_SECONDS,
    )
    response = client.patch(
        _path(team_context, f"songs/{song.pk}/"),
        {"start_time_seconds": 25, "playback_speed": 1.25},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.OK
    song.refresh_from_db()
    assert (song.start_time_seconds, song.playback_speed) == (25, 1.25)
    personal.refresh_from_db()
    assert personal.start_time_seconds == PERSONAL_START_SECONDS
    cached = song.cached_song
    assert cached is not None
    cached.status = PlayerSongStatus.FAILED
    cached.error_message = "Transient provider failure"
    cached.save()
    retry = client.post(_path(team_context, f"songs/{song.pk}/retry/"))
    assert retry.status_code == HTTPStatus.OK
    assert retry.json()["status"] == PlayerSongStatus.QUEUED
    assert not retry.json()["error_message"]
    response = client.delete(_path(team_context, f"songs/{song.pk}/"))
    assert response.status_code == HTTPStatus.NO_CONTENT
    team_context.team_data.refresh_from_db()
    assert str(song.pk) not in team_context.team_data.fallback_goal_song_song_ids
    assert PlayerSong.objects.filter(pk=personal.pk).exists()


@pytest.mark.parametrize("invalid_id", ["not-a-uuid", "123", "x" * 100])
@pytest.mark.parametrize("target", ["fallback", "player"])
def test_song_selection_rejects_malformed_uuids_without_writing(
    client: Client, team_context: TeamTestContext, invalid_id: str, target: str
) -> None:
    """Malformed selection IDs must return field validation, not ORM server errors."""
    suffix = (
        "fallback/" if target == "fallback" else f"player/{team_context.player.pk}/"
    )
    field = (
        "fallback_goal_song_song_ids" if target == "fallback" else "goal_song_song_ids"
    )
    response = client.patch(
        _path(team_context, suffix),
        data={field: [invalid_id]},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()[field] == "Must be a valid UUID."
    team_context.player.refresh_from_db()
    team_context.team_data.refresh_from_db()
    assert team_context.player.goal_song_song_ids == []
    assert team_context.team_data.fallback_goal_song_song_ids == []


def test_ready_team_song_retry_is_a_state_conflict(
    client: Client, team_context: TeamTestContext
) -> None:
    """A valid retry request cannot run against an already completed download."""
    song = _import(client, team_context)
    cached = song.cached_song
    assert cached is not None
    cached.status = PlayerSongStatus.READY
    cached.save(update_fields=["status"])
    response = client.post(_path(team_context, f"songs/{song.pk}/retry/"))
    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["code"] == "song_already_ready"
    song.refresh_from_db()
    assert song.effective_status == PlayerSongStatus.READY
