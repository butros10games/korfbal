# ruff: noqa: D103
"""Contract tests for the player background-job pipeline."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import override_settings
from django.utils import timezone
import pytest

from apps.game_tracker.tests.tracker_test_helpers import (
    create_tracker_match,
    create_tracker_player,
)
from apps.player.models import CachedSong, PlayerSong, PlayerSongStatus
from apps.player.services.match_notifications import (
    FinishedMatchJobs,
    handle_finished_match,
)
from apps.player.tasks import (
    download_cached_song,
    download_player_song,
    handle_match_finished,
    publish_mvp_and_notify,
    send_mvp_vote_reminder,
)


def test_player_tasks_keep_their_public_celery_names() -> None:
    assert handle_match_finished.name == "apps.player.tasks.handle_match_finished"
    assert send_mvp_vote_reminder.name == "apps.player.tasks.send_mvp_vote_reminder"
    assert publish_mvp_and_notify.name == "apps.player.tasks.publish_mvp_and_notify"
    assert download_cached_song.name == "apps.player.tasks.download_cached_song"
    assert download_player_song.name == "apps.player.tasks.download_player_song"


@pytest.mark.django_db
def test_cached_player_song_dispatches_the_shared_download_job() -> None:
    player = create_tracker_player(username="cached-pipeline-song")
    cached = CachedSong.objects.create(
        spotify_url="https://open.spotify.com/track/27CXrzqx1N44o1Pi6AHRT4"
    )
    song = PlayerSong.objects.create(player=player, cached_song=cached)

    with patch("apps.player.tasks.download_cached_song.apply") as dispatch:
        result = download_player_song.apply(args=[str(song.id_uuid)])

    assert result.successful()
    dispatch.assert_called_once_with(args=[str(cached.id_uuid)])


@pytest.mark.django_db
@override_settings(TESTING=False)
def test_legacy_player_song_failure_is_persisted_and_reraised() -> None:
    player = create_tracker_player(username="failed-pipeline-song")
    song = PlayerSong.objects.create(
        player=player,
        spotify_url="https://open.spotify.com/track/27CXrzqx1N44o1Pi6AHRT4",
    )

    with (
        patch(
            "apps.player.tasks.download_spotify_track",
            side_effect=RuntimeError("provider unavailable"),
        ),
        pytest.raises(RuntimeError, match="provider unavailable"),
    ):
        download_player_song.apply(args=[str(song.id_uuid)], throw=True)

    song.refresh_from_db()
    assert song.status == PlayerSongStatus.FAILED
    assert song.error_message == "provider unavailable"


@pytest.mark.django_db
def test_finished_match_claims_and_schedules_the_mvp_lifecycle() -> None:
    tracker = create_tracker_match(prefix="Player pipeline")
    tracker.match_data.status = "finished"
    tracker.match_data.save(update_fields=["status"])
    closes_at = timezone.now() + timedelta(hours=2)
    claim_once = Mock(return_value=True)
    send_payload = Mock()
    schedule_reminder = Mock()
    schedule_publish = Mock()

    with patch(
        "apps.player.services.match_notifications.mvp_service.get_or_create_match_mvp",
        return_value=SimpleNamespace(closes_at=closes_at),
    ):
        handle_finished_match(
            match_id=str(tracker.match.id_uuid),
            match_data_id=str(tracker.match_data.id_uuid),
            jobs=FinishedMatchJobs(
                claim_once=claim_once,
                send_payload=send_payload,
                schedule_reminder=schedule_reminder,
                schedule_publish=schedule_publish,
            ),
        )

    claim_once.assert_called_once_with(
        f"push:match_finished:{tracker.match_data.id_uuid}",
        60 * 60 * 24,
    )
    send_payload.assert_called_once()
    schedule_reminder.assert_called_once_with(
        match_id=str(tracker.match.id_uuid),
        eta=closes_at - timedelta(hours=1),
    )
    schedule_publish.assert_called_once_with(
        match_id=str(tracker.match.id_uuid),
        eta=closes_at + timedelta(minutes=1),
    )
