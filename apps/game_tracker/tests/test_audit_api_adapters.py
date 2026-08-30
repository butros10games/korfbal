"""Audit coverage for the match-tracker outbound and realtime adapters."""

from __future__ import annotations

from unittest.mock import Mock, patch

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


def test_celery_dispatcher_routes_immediate_and_delayed_recomputes() -> None:
    """Countdowns select the Celery primitive without changing task arguments."""
    dispatcher = CeleryTrackerJobDispatcher()
    impact_task = Mock()
    minutes_task = Mock()

    with patch.object(
        dispatcher,
        "_task",
        side_effect=[impact_task, minutes_task],
    ) as task_lookup:
        dispatcher.recompute_impacts(match_data_id="impact-id")
        dispatcher.recompute_minutes(
            match_data_id="minutes-id",
            countdown_seconds=12,
        )

    assert task_lookup.call_args_list == [
        (("recompute_match_impacts",),),
        (("recompute_match_minutes",),),
    ]
    impact_task.delay.assert_called_once_with("impact-id")
    impact_task.apply_async.assert_not_called()
    minutes_task.apply_async.assert_called_once_with(
        args=("minutes-id",),
        countdown=12,
    )
    minutes_task.delay.assert_not_called()


def test_celery_dispatcher_routes_match_finished_to_player_task() -> None:
    """Post-match work belongs to the player worker and preserves named IDs."""
    finished_task = Mock()

    with patch(
        "apps.game_tracker.adapters.outbound.runtime._task",
        return_value=finished_task,
    ) as task_lookup:
        CeleryTrackerJobDispatcher().match_finished(
            match_id="match-id",
            match_data_id="data-id",
        )

    task_lookup.assert_called_once_with("apps.player.tasks", "handle_match_finished")
    finished_task.delay.assert_called_once_with(
        match_id="match-id",
        match_data_id="data-id",
    )


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
