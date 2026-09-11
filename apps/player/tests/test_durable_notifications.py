"""Publication and delivery remain separate across retries and API reads."""

from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone
import pytest

from apps.awards.services.mvp import ensure_mvp_published, get_or_create_match_mvp
from apps.game_tracker.models import MatchPlayer
from apps.game_tracker.tests.tracker_test_helpers import (
    create_tracker_match,
    create_tracker_player,
)
from apps.kwt_common.models import BackgroundJob
from apps.kwt_common.tasks import execute_job
from apps.player.models import PlayerPushSubscription
from apps.player.tasks import handle_match_finished, publish_mvp_and_notify


pytestmark = pytest.mark.django_db
DELIVERY_TASK = "apps.player.tasks.deliver_notification"


def test_api_publication_cannot_suppress_mvp_delivery() -> None:
    """Publishing through a GET before the scheduled task still produces one intent."""
    tracker = create_tracker_match(prefix="Durable MVP")
    tracker.match_data.status = "finished"
    tracker.match_data.save(update_fields=["status"])
    player = create_tracker_player(username="durable-mvp-voter")
    MatchPlayer.objects.create(
        match_data=tracker.match_data, player=player, team=tracker.home_team
    )
    PlayerPushSubscription.objects.create(
        user_id=player.user_id, endpoint="https://example.com/mvp", subscription={}
    )
    mvp = get_or_create_match_mvp(tracker.match, tracker.match_data)
    mvp.finished_at = timezone.now() - timedelta(hours=4)
    mvp.closes_at = timezone.now() - timedelta(hours=1)
    mvp.save()
    ensure_mvp_published(tracker.match, tracker.match_data)
    for _ in range(2):
        publish_mvp_and_notify.run(match_id=str(tracker.match.pk))
    delivery = BackgroundJob.objects.get(task=DELIVERY_TASK)
    with patch("apps.player.tasks.send_web_push") as send:
        execute_job.run(delivery.pk)
        publish_mvp_and_notify.run(match_id=str(tracker.match.pk))
        execute_job.run(delivery.pk)
    send.assert_called_once()
    delivery.refresh_from_db()
    assert delivery.kwargs == {}  # Completed payloads are discarded; keys remain.


def test_delivery_failure_does_not_prevent_mvp_scheduling() -> None:
    """The match lifecycle records its deadlines before providers are contacted."""
    tracker = create_tracker_match(prefix="Durable finish")
    tracker.match_data.status = "finished"
    tracker.match_data.save(update_fields=["status"])
    player = create_tracker_player(username="durable-finish-voter")
    MatchPlayer.objects.create(
        match_data=tracker.match_data, player=player, team=tracker.home_team
    )
    PlayerPushSubscription.objects.create(
        user_id=player.user_id, endpoint="https://example.com/finish", subscription={}
    )
    handle_match_finished.run(
        match_id=str(tracker.match.pk), match_data_id=str(tracker.match_data.pk)
    )
    delivery = BackgroundJob.objects.get(task=DELIVERY_TASK)
    with (
        patch("apps.player.tasks.send_web_push", side_effect=ConnectionError),
        pytest.raises(ConnectionError),
    ):
        execute_job.run(delivery.pk)
    assert BackgroundJob.objects.filter(
        task="apps.player.tasks.send_mvp_vote_reminder"
    ).exists()
    assert BackgroundJob.objects.filter(
        task="apps.player.tasks.publish_mvp_and_notify"
    ).exists()
    delivery.refresh_from_db()
    assert delivery.due_at > timezone.now()
