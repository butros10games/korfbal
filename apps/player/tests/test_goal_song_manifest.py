"""Tests for the match tracker's consolidated goal-audio manifest."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import timedelta
from typing import Any, cast
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from django.core.files.base import ContentFile
from django.utils import timezone
import pytest
from pytest_django.fixtures import DjangoAssertNumQueries

from apps.game_tracker.tests.tracker_test_helpers import (
    TrackerMatchContext,
    create_tracker_match,
    create_tracker_player,
)
from apps.player.models import (
    CachedSong,
    Player,
    PlayerGoalSongSelection,
    PlayerSong,
    PlayerSongStatus,
    TeamGoalSongSelection,
)
from apps.player.services.goal_song_manifest import build_goal_song_manifest
from apps.schedule.models import Season
from apps.team.models import TeamData


@pytest.mark.django_db
def test_goal_song_manifest_preserves_player_and_fallback_selection_order() -> None:
    """The manifest should preserve configured cycling order with stable URLs."""
    tracker = create_tracker_match(prefix="Goal audio manifest")
    scorer = create_tracker_player(username="manifest-scorer")
    fallback_player = create_tracker_player(username="manifest-fallback")

    first = PlayerSong.objects.create(
        player=scorer,
        status=PlayerSongStatus.READY,
        start_time_seconds=7,
        playback_speed=1.1,
    )
    first.audio_file.save("first.mp3", ContentFile(b"first"), save=True)
    second = PlayerSong.objects.create(
        player=scorer,
        status=PlayerSongStatus.READY,
        start_time_seconds=3,
        clip_duration_seconds=6,
        audio_file=first.audio_file.name,
    )
    fallback = PlayerSong.objects.create(
        player=fallback_player,
        status=PlayerSongStatus.READY,
        start_time_seconds=11,
    )
    fallback.audio_file.save("fallback.mp3", ContentFile(b"fallback"), save=True)

    scorer.goal_song_song_ids = [str(second.id_uuid), str(first.id_uuid)]
    scorer.save(update_fields=["goal_song_song_ids"])
    team_data = TeamData.objects.create(
        team=tracker.home_team,
        season=tracker.match.season,
        fallback_goal_song_song_ids=[str(fallback.id_uuid), str(second.id_uuid)],
    )
    team_data.players.add(scorer, fallback_player)

    manifest = build_goal_song_manifest(
        player_ids=[str(scorer.id_uuid)],
        team=tracker.home_team,
        season=tracker.match.season,
    )

    players = cast(dict[str, list[dict[str, Any]]], manifest["players"])
    entries = players[str(scorer.id_uuid)]
    assert [entry["id"] for entry in entries] == [
        str(second.id_uuid),
        str(first.id_uuid),
    ]
    assert str(entries[0]["url"]).startswith("/api/player/api/songs/")
    assert "start=3" in str(entries[0]["url"])
    assert "duration=6" in str(entries[0]["url"])
    assert "duration=8" in str(entries[1]["url"])
    assert entries[0]["url"] != entries[1]["url"]
    assert first.source_id == second.source_id
    assert "stream=1" in str(entries[0]["url"])
    assert "media." not in str(entries[0]["url"])
    fallback_entries = cast(list[dict[str, Any]], manifest["fallback"])
    assert [entry["id"] for entry in fallback_entries] == [
        str(fallback.id_uuid),
        str(second.id_uuid),
    ]


@pytest.mark.django_db
def test_goal_song_manifest_omits_unready_and_wrong_owner_songs() -> None:
    """The manifest must expose only ready songs owned by the selected player."""
    tracker = create_tracker_match(prefix="Goal audio filtering")
    scorer = create_tracker_player(username="manifest-filter-scorer")
    other = create_tracker_player(username="manifest-filter-other")
    unready = PlayerSong.objects.create(player=scorer)
    wrong_owner = PlayerSong.objects.create(
        player=other,
        status=PlayerSongStatus.READY,
    )
    wrong_owner.audio_file.save("wrong.mp3", ContentFile(b"wrong"), save=True)
    scorer.goal_song_song_ids = [str(unready.id_uuid), str(wrong_owner.id_uuid)]
    scorer.save(update_fields=["goal_song_song_ids"])

    manifest = build_goal_song_manifest(
        player_ids=[str(scorer.id_uuid)],
        team=tracker.home_team,
        season=tracker.match.season,
    )

    assert manifest == {"version": 1, "players": {}, "fallback": []}


@pytest.fixture
def selected_audio() -> tuple[TrackerMatchContext, Player, PlayerSong, TeamData]:
    """Select one synthetic clip for its owner and the seasonal team."""
    tracker = create_tracker_match(prefix="Selected audio")
    player = create_tracker_player(username="selected-audio-owner")
    song = PlayerSong.objects.create(
        player=player,
        status=PlayerSongStatus.READY,
        audio_file="synthetic/selected.mp3",
        start_time_seconds=7,
        playback_speed=1.1,
    )
    player.goal_song_song_ids = [str(song.pk)]
    player.save(update_fields=["goal_song_song_ids"])
    team_data = TeamData.objects.create(
        team=tracker.home_team,
        season=tracker.match.season,
        fallback_goal_song_song_ids=[str(song.pk)],
    )
    team_data.players.add(player)
    return tracker, player, song, team_data


@pytest.mark.django_db
@pytest.mark.parametrize("with_audio", [False, True])
def test_manifest_reads_only_selection_ids_and_effective_songs(
    selected_audio: tuple[TrackerMatchContext, Player, PlayerSong, TeamData],
    django_assert_num_queries: DjangoAssertNumQueries,
    with_audio: bool,
) -> None:
    """Avoid owner/selection hydration and unbounded team membership reads."""
    tracker, player, _song, team_data = selected_audio
    if not with_audio:
        player.goal_song_selections.all().delete()
        team_data.goal_song_selections.all().delete()
    with ExitStack() as stack:
        spies = []
        for model in (Player, TeamData, PlayerGoalSongSelection, TeamGoalSongSelection):
            spy = Mock(wraps=model.from_db.__func__)
            stack.enter_context(patch.object(model, "from_db", classmethod(spy)))
            spies.append(spy)
        with django_assert_num_queries(4 if with_audio else 2):
            manifest = build_goal_song_manifest(
                player_ids=[str(player.pk)],
                team=tracker.home_team,
                season=tracker.match.season,
            )
        for spy in spies:
            spy.assert_not_called()
    assert bool(manifest["players"]) == with_audio
    assert bool(manifest["fallback"]) == with_audio


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("identity", "visible"),
    [
        ("account", True),
        ("local", True),
        ("OPEN", True),
        ("NORMAL", True),
        ("LIMITED", True),
        ("PRIVATE", False),
        ("stale", False),
        ("unobserved", False),
        ("archived", False),
    ],
)
def test_manifest_preserves_player_and_fallback_owner_privacy(
    selected_audio: tuple[TrackerMatchContext, Player, PlayerSong, TeamData],
    identity: str,
    visible: bool,
) -> None:
    """Direct selection reads must retain the normal Player manager's policy."""
    tracker, player, song, _ = selected_audio
    updates: dict[str, Any] = {
        "knkv_person_id": "synthetic-manifest-person",
        "knkv_privacy": identity,
        "knkv_observed_at": timezone.now(),
    }
    if identity != "account":
        updates["user_id"] = None
    if identity == "local":
        updates["knkv_person_id"] = None
    if identity == "stale":
        updates.update(
            knkv_privacy="OPEN",
            knkv_observed_at=timezone.now() - timedelta(days=9),
        )
    if identity == "unobserved":
        updates.update(knkv_privacy="OPEN", knkv_observed_at=None)
    if identity == "archived":
        updates["archived_at"] = timezone.now()
    Player.all_objects.filter(pk=player.pk).update(**updates)

    manifest = build_goal_song_manifest(
        player_ids=[str(player.pk)], team=tracker.home_team, season=tracker.match.season
    )
    assert bool(manifest["players"]) == visible
    fallback = cast(list[dict[str, Any]], manifest["fallback"])
    assert [entry["id"] for entry in fallback] == ([str(song.pk)] if visible else [])


@pytest.mark.django_db
def test_manifest_skips_wrong_owner_and_nonmember_songs_before_hydration(
    selected_audio: tuple[TrackerMatchContext, Player, PlayerSong, TeamData],
) -> None:
    """Invalid selections must not load audio metadata for unrelated owners."""
    tracker, player, song, team_data = selected_audio
    other = create_tracker_player(username="manifest-unrelated-owner")
    other.goal_song_song_ids = [str(song.pk)]
    other.save(update_fields=["goal_song_song_ids"])
    team_data.players.remove(player)
    spy = Mock(wraps=PlayerSong.from_db.__func__)
    with patch.object(PlayerSong, "from_db", classmethod(spy)):
        manifest = build_goal_song_manifest(
            player_ids=[str(other.pk)],
            team=tracker.home_team,
            season=tracker.match.season,
        )
    assert manifest == {"version": 1, "players": {}, "fallback": []}
    spy.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize("selection", ["exact", "latest", "latest_empty", "missing"])
def test_manifest_uses_only_the_selected_seasonal_team(
    selected_audio: tuple[TrackerMatchContext, Player, PlayerSong, TeamData],
    selection: str,
) -> None:
    """Latest-season selection must not fall back to an older nonempty selection."""
    tracker, player, old_song, team_data = selected_audio
    season = Season.objects.create(
        name="Later manifest season",
        start_date=team_data.season.start_date + timedelta(days=500),
        end_date=team_data.season.end_date + timedelta(days=500),
    )
    song = PlayerSong.objects.create(
        player=player, status=PlayerSongStatus.READY, audio_file="synthetic/later.mp3"
    )
    latest = TeamData.objects.create(
        team=tracker.home_team,
        season=season,
        fallback_goal_song_song_ids=[str(song.pk)],
    )
    latest.players.add(player)
    # Another team's selection in the same season must never be used.
    other = TeamData.objects.create(
        team=tracker.away_team,
        season=season,
        fallback_goal_song_song_ids=[str(song.pk)],
    )
    other.players.add(player)
    if selection == "latest_empty":
        latest.goal_song_selections.all().delete()
    if selection == "missing":
        latest.delete()
    requested_season = (
        tracker.match.season
        if selection == "exact"
        else season
        if selection == "missing"
        else None
    )
    manifest = build_goal_song_manifest(
        player_ids=[], team=tracker.home_team, season=requested_season
    )
    expected = {
        "exact": [str(old_song.pk)],
        "latest": [str(song.pk)],
        "latest_empty": [],
        "missing": [],
    }
    fallback = cast(list[dict[str, Any]], manifest["fallback"])
    assert [entry["id"] for entry in fallback] == expected[selection]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("cached_status", "cached_file", "ready"),
    [
        ("ready", "synthetic/cached.mp3", True),
        ("queued", "", False),
        ("ready", "", False),
        ("failed", "synthetic/cached.mp3", False),
    ],
)
def test_manifest_uses_cached_audio_readiness_and_version(
    selected_audio: tuple[TrackerMatchContext, Player, PlayerSong, TeamData],
    cached_status: str,
    cached_file: str,
    ready: bool,
) -> None:
    """A shared source overrides local readiness, retaining per-player playback."""
    tracker, player, song, _ = selected_audio
    cached = CachedSong.objects.create(
        spotify_url="https://example.invalid/manifest-source",
        status=cached_status,
        audio_file=cached_file,
    )
    song.cached_song = cached
    song.status = PlayerSongStatus.QUEUED if ready else PlayerSongStatus.READY
    song.save(update_fields=["cached_song", "status"])
    manifest = build_goal_song_manifest(
        player_ids=[str(player.pk)], team=tracker.home_team, season=tracker.match.season
    )
    assert bool(manifest["players"]) == ready
    fallback = cast(list[dict[str, Any]], manifest["fallback"])
    assert bool(fallback) == ready
    if ready:
        query = parse_qs(urlsplit(fallback[0]["url"]).query)
        assert query == {
            "start": ["7"],
            "duration": ["8"],
            "stream": ["1"],
            "v": [f"{cached_file}:{int(cached.updated_at.timestamp() * 1_000_000)}"],
        }
        assert fallback[0]["playback_speed"] == pytest.approx(song.playback_speed)
        CachedSong.objects.filter(pk=cached.pk).update(
            audio_file="synthetic/replaced.mp3",
            updated_at=cached.updated_at + timedelta(seconds=1),
        )
        refreshed = build_goal_song_manifest(
            player_ids=[str(player.pk)],
            team=tracker.home_team,
            season=tracker.match.season,
        )
        assert refreshed["fallback"] != fallback


@pytest.mark.django_db
def test_manifest_retains_requested_player_order_and_deduplicates_ids(
    selected_audio: tuple[TrackerMatchContext, Player, PlayerSong, TeamData],
) -> None:
    """Database selection ordering must not reorder the requested player map."""
    tracker, player, _, _ = selected_audio
    other = create_tracker_player(username="manifest-second-scorer")
    song = PlayerSong.objects.create(
        player=other, status=PlayerSongStatus.READY, audio_file="synthetic/other.mp3"
    )
    other.goal_song_song_ids = [str(song.pk)]
    other.save(update_fields=["goal_song_song_ids"])
    player_ids = [str(other.pk), str(player.pk), str(other.pk)]
    manifest = build_goal_song_manifest(
        player_ids=iter(player_ids), team=tracker.home_team, season=tracker.match.season
    )
    assert list(cast(dict[str, Any], manifest["players"])) == player_ids[:2]


@pytest.mark.django_db
def test_manifest_accepts_team_owned_clips_only_for_the_selected_season_team() -> None:
    """A team selection must not expose another team's owned source."""
    tracker = create_tracker_match(prefix="Team-owned manifest")
    home = TeamData.objects.create(team=tracker.home_team, season=tracker.match.season)
    away = TeamData.objects.create(team=tracker.away_team, season=tracker.match.season)
    own = PlayerSong.objects.create(
        team_data=home,
        status=PlayerSongStatus.READY,
        audio_file="synthetic/team.mp3",
        clip_duration_seconds=12,
    )
    foreign = PlayerSong.objects.create(
        team_data=away,
        status=PlayerSongStatus.READY,
        audio_file="synthetic/other-team.mp3",
    )
    home.fallback_goal_song_song_ids = [str(own.pk), str(foreign.pk)]
    home.save(update_fields=["fallback_goal_song_song_ids"])
    manifest = build_goal_song_manifest(
        player_ids=[],
        team=tracker.home_team,
        season=tracker.match.season,
    )
    entries = cast(list[dict[str, Any]], manifest["fallback"])
    assert [entry["id"] for entry in entries] == [str(own.pk)]
    assert "duration=12" in str(entries[0]["url"])
