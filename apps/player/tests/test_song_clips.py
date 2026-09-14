"""Independent clip settings, owner boundaries and shared audio lifecycle."""

from http import HTTPStatus
from unittest.mock import Mock

from django.core.files.base import ContentFile
from django.test import Client
import pytest

from apps.kwt_common.models import BackgroundJob
from apps.player.api.serializers import PlayerSongSerializer
from apps.player.application.ports import AudioRuntime
from apps.player.models import CachedSong, PlayerSong, PlayerSongStatus
from apps.player.services.player_audio import prepare_player_song_clip
from apps.team.tests.team_test_support import TeamTestContext, build_team_context


pytestmark = pytest.mark.django_db
SOURCE = "https://www.youtube.com/watch?v=BaW_jenozKc"
CLIP_START = 24
CLIP_LENGTH = 6
SONG_LENGTH = 90


@pytest.fixture
def context(client: Client) -> TeamTestContext:
    """Authorize a coach for personal and roster-scoped clip commands."""
    context = build_team_context(suffix="multiple-clips")
    client.force_login(context.coach.user)
    return context


def _path(context: TeamTestContext, song: PlayerSong, owner: str) -> str:
    if owner == "player":
        return f"/api/player/me/songs/{song.pk}/"
    prefix = f"/api/team/teams/{context.team.pk}/goal-song-admin/"
    if owner == "roster":
        prefix += f"player/{song.player_id}/"
    return f"{prefix}songs/{song.pk}/"


def _source(context: TeamTestContext, owner: str, kind: str) -> PlayerSong:
    song = PlayerSong.objects.create(
        player=None
        if owner == "team"
        else (context.player if owner == "roster" else context.coach),
        team_data=context.team_data if owner == "team" else None,
        title="Original track",
        duration_seconds=SONG_LENGTH,
        start_time_seconds=8,
        status=PlayerSongStatus.READY,
    )
    if kind == "import":
        cached = CachedSong.objects.create(
            spotify_url=SOURCE,
            title="Original track",
            duration_seconds=SONG_LENGTH,
            status=PlayerSongStatus.READY,
        )
        cached.audio_file.save("source.mp3", ContentFile(b"ID3synthetic"), save=True)
        song.cached_song = cached
        song.spotify_url = SOURCE
        song.save()
    else:
        song.audio_file.save("source.mp3", ContentFile(b"ID3synthetic"), save=True)
    return song


@pytest.mark.parametrize("owner", ["player", "team", "roster"])
@pytest.mark.parametrize("kind", ["import", "upload"])
def test_create_edit_and_delete_clips_preserves_shared_audio(
    client: Client, context: TeamTestContext, owner: str, kind: str
) -> None:
    """All owners can reuse audio and edit or delete clips independently."""
    source = _source(context, owner, kind)
    original = PlayerSongSerializer(source).data
    path = _path(context, source, owner)
    response = client.post(
        path + f"clips/?season={context.season.pk}",
        {
            "clip_name": "Refrein",
            "start_time_seconds": CLIP_START,
            "clip_duration_seconds": CLIP_LENGTH,
        },
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.CREATED, response.content
    clip = PlayerSong.objects.select_related("cached_song").get(
        pk=response.json()["id_uuid"]
    )
    assert clip.pk != source.pk
    assert response.json()["source_id"] == original["source_id"]
    assert clip.effective_audio_file.name == source.effective_audio_file.name
    assert response.json()["clip_name"] == "Refrein"
    assert clip.start_time_seconds == CLIP_START
    assert clip.clip_duration_seconds == CLIP_LENGTH
    assert BackgroundJob.objects.filter(
        task="apps.player.tasks.download_player_song", args=[str(clip.pk)]
    ).exists()
    assert not BackgroundJob.objects.filter(
        task="apps.player.tasks.download_cached_song"
    ).exists()
    source.refresh_from_db()
    assert PlayerSongSerializer(source).data == original
    edit_path = _path(context, clip, owner) + ("settings/" if owner == "roster" else "")
    edit = client.patch(
        edit_path + f"?season={context.season.pk}",
        {"clip_name": "Finale", "clip_duration_seconds": 5},
        content_type="application/json",
    )
    assert edit.status_code == HTTPStatus.OK, edit.content
    source.refresh_from_db()
    assert PlayerSongSerializer(source).data == original
    deleted = client.delete(path + f"?season={context.season.pk}")
    assert deleted.status_code == HTTPStatus.NO_CONTENT
    clip.refresh_from_db()
    assert clip.source_id == original["source_id"]
    with clip.effective_audio_file.open("rb") as audio:
        assert audio.read() == b"ID3synthetic"
    assert CachedSong.objects.count() == (1 if kind == "import" else 0)


@pytest.mark.parametrize("owner", ["player", "team", "roster"])
@pytest.mark.parametrize(
    "payload",
    [
        {"start_time_seconds": -1},
        {"clip_duration_seconds": 0},
        {"clip_duration_seconds": 16},
        {"start_time_seconds": 89, "clip_duration_seconds": 8},
        {"clip_name": "x" * 81},
    ],
)
def test_clip_validation_is_consistent_across_owner_routes(
    client: Client, context: TeamTestContext, owner: str, payload: dict
) -> None:
    """Invalid settings cannot create a clip or dispatch work."""
    source = _source(context, owner, "import")
    response = client.post(
        _path(context, source, owner) + f"clips/?season={context.season.pk}",
        payload,
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert PlayerSong.objects.count() == 1
    assert not BackgroundJob.objects.exists()


def test_clip_creation_does_not_accept_another_players_source(
    client: Client, context: TeamTestContext
) -> None:
    """A guessed source UUID is never sufficient to clone somebody else's media."""
    source = _source(context, "roster", "upload")
    response = client.post(
        f"/api/player/me/songs/{source.pk}/clips/", {}, content_type="application/json"
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    response = client.post(
        f"/api/team/teams/{context.team.pk}/goal-song-admin/songs/{source.pk}/clips/?season={context.season.pk}",
        {},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert PlayerSong.objects.count() == 1


def test_clip_creation_requires_a_ready_source(
    client: Client, context: TeamTestContext
) -> None:
    """Pending imports cannot create unusable dependent clips."""
    source = _source(context, "player", "import")
    source.cached_song.status = PlayerSongStatus.FAILED
    source.cached_song.save()
    response = client.post(
        f"/api/player/me/songs/{source.pk}/clips/", {}, content_type="application/json"
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert PlayerSong.objects.count() == 1


def test_import_remains_idempotent_after_multiple_clips(
    client: Client, context: TeamTestContext
) -> None:
    """Reimporting a link stays idempotent, even after deleting the original clip."""
    source = _source(context, "player", "import")
    response = client.post(
        f"/api/player/me/songs/{source.pk}/clips/",
        {"clip_name": "Refrein"},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.CREATED
    again = client.post(
        "/api/player/me/songs/", {"source_url": SOURCE}, content_type="application/json"
    )
    assert again.status_code == HTTPStatus.OK
    assert again.json()["id_uuid"] == str(source.pk)
    source.delete()
    again = client.post(
        "/api/player/me/songs/", {"source_url": SOURCE}, content_type="application/json"
    )
    assert again.status_code == HTTPStatus.OK
    assert again.json()["id_uuid"] == response.json()["id_uuid"]


def test_worker_prepares_the_configured_clip_duration(context: TeamTestContext) -> None:
    """Worker storage keys must match the length requested by the tracker manifest."""
    source = _source(context, "player", "upload")
    source.clip_duration_seconds = CLIP_LENGTH
    storage = Mock()
    storage.exists.return_value = True
    key = prepare_player_song_clip(
        source, runtime=AudioRuntime(storage=storage, commands=Mock())
    )
    assert key is not None
    assert f"dur_{CLIP_LENGTH}.mp3" in key


@pytest.mark.parametrize("owner", ["team", "roster"])
@pytest.mark.parametrize("access", ["anonymous", "roster_only", "wrong_season"])
def test_clip_creation_requires_team_management_in_selected_season(
    client: Client, context: TeamTestContext, owner: str, access: str
) -> None:
    """Clip creation retains the moderation boundary on both team routes."""
    source = _source(context, owner, "upload")
    season_id = str(context.season.pk)
    if access == "anonymous":
        client.logout()
    elif access == "roster_only":
        client.force_login(context.player.user)
    else:
        season_id = "11111111-1111-4111-8111-111111111111"
    response = client.post(
        _path(context, source, owner) + f"clips/?season={season_id}",
        {},
        content_type="application/json",
    )
    assert response.status_code in {
        HTTPStatus.BAD_REQUEST,
        HTTPStatus.UNAUTHORIZED,
        HTTPStatus.FORBIDDEN,
        HTTPStatus.NOT_FOUND,
    }
    assert PlayerSong.objects.count() == 1
    assert not BackgroundJob.objects.exists()
