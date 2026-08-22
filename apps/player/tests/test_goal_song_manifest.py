"""Tests for the match tracker's consolidated goal-audio manifest."""

from __future__ import annotations

from typing import Any, cast

from django.core.files.base import ContentFile
import pytest

from apps.game_tracker.tests.tracker_test_helpers import (
    create_tracker_match,
    create_tracker_player,
)
from apps.player.models import PlayerSong, PlayerSongStatus
from apps.player.services.goal_song_manifest import build_goal_song_manifest
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
    )
    second.audio_file.save("second.mp3", ContentFile(b"second"), save=True)
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
        fallback_goal_song_song_ids=[str(fallback.id_uuid)],
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
    assert "duration=8" in str(entries[0]["url"])
    assert "stream=1" in str(entries[0]["url"])
    assert "media." not in str(entries[0]["url"])
    fallback_entries = cast(list[dict[str, Any]], manifest["fallback"])
    assert fallback_entries[0]["id"] == str(fallback.id_uuid)


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
