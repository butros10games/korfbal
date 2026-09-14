"""Shared team-clip discovery and independent recipient ownership."""

from http import HTTPStatus

from django.core.files.base import ContentFile
from django.test import Client
import pytest

from apps.kwt_common.models import BackgroundJob
from apps.player.models import PlayerSong, PlayerSongStatus
from apps.player.models.cached_song import CachedSong
from apps.team.tests.team_test_support import (
    TeamTestContext,
    build_team_context,
    create_song,
)


pytestmark = pytest.mark.django_db
PAGE_SIZE = 20
TOTAL_CLIPS = 23
EDITED_START = 20


@pytest.fixture
def recipient(client: Client) -> TeamTestContext:
    """Authorize only the receiving team's coach."""
    context = build_team_context(suffix="recipient")
    assert context.coach.user is not None
    client.force_login(context.coach.user)
    return context


@pytest.fixture
def source() -> PlayerSong:
    """Create a ready uploaded clip owned by a different team's season."""
    context = build_team_context(suffix="origin")
    song = PlayerSong.objects.create(
        team_data=context.team_data,
        title="Victory",
        clip_name="Refrein",
        status=PlayerSongStatus.READY,
        start_time_seconds=12,
        clip_duration_seconds=6,
        playback_speed=1.2,
    )
    song.audio_file.save("victory.mp3", ContentFile(b"ID3synthetic-audio"), save=True)
    return song


def path(context: TeamTestContext, *, add: bool = False) -> str:
    """Build the team- and season-scoped library route."""
    suffix = "add/" if add else ""
    return (
        f"/api/team/teams/{context.team.pk}/goal-song-admin/library/{suffix}"
        f"?season={context.season.pk}"
    )


def test_search_is_paginated_and_does_not_filter_the_recipient_team(
    client: Client, recipient: TeamTestContext, source: PlayerSong
) -> None:
    """Song and source club search must not become a team-detail lookup filter."""
    create_song(player=recipient.player, title="Private Victory")
    PlayerSong.objects.create(
        team_data=recipient.team_data,
        title="Missing audio",
        status=PlayerSongStatus.READY,
    )
    response = client.get(path(recipient), {"search": "origin Refrein"})
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["count"] == 1
    assert payload["results"][0]["id_uuid"] == str(source.pk)
    assert payload["results"][0]["club_name"] == "Club origin"
    assert payload["results"][0]["already_added"] is False
    for index in range(TOTAL_CLIPS - 1):
        PlayerSong.objects.create(
            team_data=source.team_data,
            title=f"Page {index}",
            status=PlayerSongStatus.READY,
            audio_file=source.audio_file.name,
        )
    first = client.get(path(recipient)).json()
    assert first["count"] == TOTAL_CLIPS
    assert len(first["results"]) == PAGE_SIZE
    second = client.get(path(recipient), {"page": 2}).json()
    assert len(second["results"]) == TOTAL_CLIPS - PAGE_SIZE
    assert {row["id_uuid"] for row in first["results"]}.isdisjoint(
        row["id_uuid"] for row in second["results"]
    )


def test_add_is_idempotent_independent_and_selected(
    client: Client, recipient: TeamTestContext, source: PlayerSong
) -> None:
    """Copy bytes into the recipient's storage and preserve timing and source life."""
    response = client.post(
        path(recipient, add=True),
        {"source_id": str(source.pk)},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.CREATED
    copied = PlayerSong.objects.get(pk=response.json()["id_uuid"])
    assert copied.player_id is None
    assert copied.team_data_id == recipient.team_data.pk
    assert copied.audio_file.name != source.audio_file.name
    assert copied.audio_file.name.startswith(f"team_songs/{recipient.team_data.pk}/")
    with copied.audio_file.open() as audio:
        assert audio.read() == b"ID3synthetic-audio"
    assert (
        copied.clip_name,
        copied.start_time_seconds,
        copied.clip_duration_seconds,
        copied.playback_speed,
    ) == ("Refrein", 12, 6, 1.2)
    recipient.team_data.refresh_from_db()
    assert recipient.team_data.fallback_goal_song_song_ids == [str(copied.pk)]
    assert BackgroundJob.objects.filter(
        task="apps.player.tasks.download_player_song", args=[str(copied.pk)]
    ).exists()
    copied.start_time_seconds = 20
    copied.save(update_fields=["start_time_seconds"])
    repeated = client.post(
        path(recipient, add=True),
        {"source_id": str(source.pk)},
        content_type="application/json",
    )
    assert repeated.status_code == HTTPStatus.OK
    assert repeated.json()["id_uuid"] == str(copied.pk)
    assert repeated.json()["start_time_seconds"] == EDITED_START
    assert client.get(path(recipient)).json()["results"][0]["already_added"] is True
    source.delete()
    copied.refresh_from_db()
    assert copied.library_source_id is None
    assert copied.audio_file.storage.exists(copied.audio_file.name)


@pytest.mark.parametrize("kind", ["personal", "unready", "own", "missing"])
def test_add_rejects_nonlibrary_sources(
    client: Client, recipient: TeamTestContext, source: PlayerSong, kind: str
) -> None:
    """Alternate add requests cannot bypass shared-library eligibility."""
    if kind == "personal":
        source.team_data = None
        source.player = recipient.player
    elif kind == "own":
        source.team_data = recipient.team_data
    elif kind == "missing":
        source.audio_file = ""
    else:
        source.status = PlayerSongStatus.QUEUED
    source.save()
    before = PlayerSong.objects.count()
    response = client.post(
        path(recipient, add=True),
        {"source_id": str(source.pk)},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert PlayerSong.objects.count() == before


def test_library_requires_recipient_management_access(
    client: Client, recipient: TeamTestContext, source: PlayerSong
) -> None:
    """Both discovery and copying require a manager of the receiving team."""
    assert recipient.player.user is not None
    client.force_login(recipient.player.user)
    assert client.get(path(recipient)).status_code == HTTPStatus.FORBIDDEN
    assert (
        client.post(
            path(recipient, add=True), {"source_id": str(source.pk)}
        ).status_code
        == HTTPStatus.FORBIDDEN
    )
    client.logout()
    assert client.get(path(recipient)).status_code in {401, 403}


def test_cached_sources_reuse_downloads_and_skip_failed_cache(
    client: Client,
    recipient: TeamTestContext,
    source: PlayerSong,
) -> None:
    """Reuse provider audio with independent team clip settings."""
    cached = CachedSong.objects.create(
        spotify_url="https://www.youtube.com/watch?v=BaW_jenozKc",
        title="Cached Victory",
        status=PlayerSongStatus.READY,
    )
    cached.audio_file.save("cache.mp3", ContentFile(b"ID3cached"), save=True)
    source.cached_song = cached
    source.save()
    response = client.post(
        path(recipient, add=True),
        {"source_id": str(source.pk)},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.CREATED
    copy = PlayerSong.objects.get(pk=response.json()["id_uuid"])
    assert copy.cached_song_id == cached.pk
    assert not copy.audio_file
    cached.status = PlayerSongStatus.FAILED
    cached.save()
    assert client.get(path(recipient)).json()["count"] == 0
    assert (
        client.post(
            path(recipient, add=True),
            {"source_id": str(source.pk)},
            content_type="application/json",
        ).status_code
        == HTTPStatus.NOT_FOUND
    )


def test_unknown_season_and_invalid_source_fail_before_copying(
    client: Client,
    recipient: TeamTestContext,
    source: PlayerSong,
) -> None:
    """Do not broaden an invalid recipient season or accept malformed source IDs."""
    url = path(recipient, add=True).split("?")[0]
    assert (
        client.post(
            f"{url}?season={source.pk}",
            {"source_id": str(source.pk)},
            content_type="application/json",
        ).status_code
        == HTTPStatus.NOT_FOUND
    )
    assert (
        client.post(
            path(recipient, add=True),
            {"source_id": "invalid"},
            content_type="application/json",
        ).status_code
        == HTTPStatus.BAD_REQUEST
    )
