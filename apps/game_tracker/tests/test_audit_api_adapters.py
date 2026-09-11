"""Audit coverage for the match-tracker outbound and realtime adapters."""

from __future__ import annotations

from unittest.mock import Mock, patch

from django.utils import timezone
import pytest

from apps.game_tracker.adapters.outbound.runtime import (
    CeleryTrackerJobDispatcher,
    ChannelsMatchChangePublisher,
)
from apps.game_tracker.realtime.contracts import LiveResource
from apps.game_tracker.realtime.publisher import (
    match_group_name,
    publish_match_changed,
)
from apps.kwt_common.models import BackgroundJob


@pytest.mark.django_db
def test_celery_dispatcher_routes_immediate_and_delayed_recomputes() -> None:
    """Both projection requests coalesce and preserve the earliest deadline."""
    dispatcher = CeleryTrackerJobDispatcher()
    dispatcher.recompute_minutes(match_data_id="data-id", countdown_seconds=12)
    job = BackgroundJob.objects.get()
    assert job.due_at > timezone.now()
    dispatcher.recompute_impacts(match_data_id="data-id")
    job.refresh_from_db()
    assert BackgroundJob.objects.count() == 1
    assert job.queue == "projections"
    assert job.args == ["data-id"]
    assert job.due_at <= timezone.now()


@pytest.mark.django_db
def test_celery_dispatcher_routes_match_finished_to_player_task() -> None:
    """Post-match lifecycle identifiers are durable and uniquely scheduled."""
    for _ in range(2):
        CeleryTrackerJobDispatcher().match_finished(
            match_id="match-id", match_data_id="data-id"
        )
    job = BackgroundJob.objects.get()
    assert job.task == "apps.player.tasks.handle_match_finished"
    assert job.kwargs == {"match_id": "match-id", "match_data_id": "data-id"}
    assert job.generation == 1


def test_channels_adapter_forwards_the_publication_contract() -> None:
    """The application port forwards all fields without reshaping resources."""
    resources = [LiveResource.LIVE, "stats"]

    with patch(
        "apps.game_tracker.adapters.outbound.runtime.publish_match_changed",
    ) as publish:
        ChannelsMatchChangePublisher().publish(
            match_id="match-id",
            revision=7,
            resources=resources,
        )

    publish.assert_called_once_with(
        match_id="match-id",
        revision=7,
        resources=resources,
    )


def test_publisher_sorts_and_deduplicates_resources() -> None:
    """Clients receive a deterministic resource list even from mixed iterables."""
    group_send = Mock()
    channel_layer = Mock(group_send=group_send)

    with (
        patch(
            "apps.game_tracker.realtime.publisher.get_channel_layer",
            return_value=channel_layer,
        ),
        patch(
            "apps.game_tracker.realtime.publisher.async_to_sync",
            side_effect=lambda function: function,
        ),
        patch("apps.game_tracker.realtime.publisher.SSE_PUBLICATIONS") as metric,
    ):
        publish_match_changed(
            match_id="match-id",
            revision=4,
            resources=[LiveResource.STATS, "live", LiveResource.STATS],
        )

    group_send.assert_called_once_with(
        match_group_name("match-id"),
        {
            "type": "match.changed",
            "match_id": "match-id",
            "revision": 4,
            "resources": ["live", "stats"],
        },
    )
    metric.labels.assert_called_once_with(result="success")
    metric.labels.return_value.inc.assert_called_once_with()


def test_publisher_fails_open_when_channel_layer_is_unavailable() -> None:
    """A missing optional transport must not roll back a committed mutation."""
    with (
        patch(
            "apps.game_tracker.realtime.publisher.get_channel_layer",
            return_value=None,
        ),
        patch("apps.game_tracker.realtime.publisher.SSE_PUBLICATIONS") as metric,
        patch("apps.game_tracker.realtime.publisher.logger") as logger,
    ):
        publish_match_changed(
            match_id="match-id",
            revision=1,
            resources=[LiveResource.LIVE],
        )

    metric.labels.assert_called_once_with(result="unavailable")
    metric.labels.return_value.inc.assert_called_once_with()
    logger.warning.assert_called_once()


@pytest.mark.parametrize("failure", [RuntimeError("Valkey unavailable")])
def test_publisher_fails_open_when_group_send_raises(failure: Exception) -> None:
    """Transport exceptions are observed but never escape into the write path."""
    channel_layer = Mock()

    with (
        patch(
            "apps.game_tracker.realtime.publisher.get_channel_layer",
            return_value=channel_layer,
        ),
        patch(
            "apps.game_tracker.realtime.publisher.async_to_sync",
            side_effect=failure,
        ),
        patch("apps.game_tracker.realtime.publisher.SSE_PUBLICATIONS") as metric,
        patch("apps.game_tracker.realtime.publisher.logger") as logger,
    ):
        publish_match_changed(
            match_id="match-id",
            revision=2,
            resources=[LiveResource.TRACKER],
        )

    metric.labels.assert_called_once_with(result="failure")
    metric.labels.return_value.inc.assert_called_once_with()
    logger.exception.assert_called_once()
