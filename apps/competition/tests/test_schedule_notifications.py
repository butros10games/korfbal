"""Future schedule changes notify followers without replaying initial imports."""

from datetime import timedelta
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
import pytest

from apps.competition.adapters.outbound.notifications import dispatch_schedule_change
from apps.competition.models import Match
from apps.competition.services.importer import Importer
from apps.competition.services.publishing import Publisher, publish_catalogue
from apps.competition.services.schedule_notifications import (
    notify_schedule_change,
    schedule_changed,
)
from apps.competition.tests.test_importer import match_payload
from apps.game_tracker.models import MatchData
from apps.kwt_common.models import BackgroundJob
from apps.player.models import Player
from apps.schedule.models import Season


@pytest.mark.django_db
def test_publication_announces_future_changes_once_and_protects_tracking(
    season: Season,
) -> None:
    """Import/replay is silent; changed time and cancellation each dispatch once."""
    payload = match_payload()
    payload.update(
        Status="SCHEDULED",
        HomeResult=None,
        AwayResult=None,
        MatchDateTime=(timezone.now() + timedelta(days=4)).isoformat(),
    )
    with patch(
        "apps.competition.services.publishing.schedule_change_dispatcher"
    ) as factory:
        dispatch = factory.return_value
        Importer(season, timezone.now()).apply(
            "club_results", "C", {"MatchResult": [payload]}
        )
        publish_catalogue()
        dispatch.assert_not_called()
        payload["MatchDateTime"] = (timezone.now() + timedelta(days=5)).isoformat()
        Importer(season, timezone.now()).apply(
            "club_results", "C", {"MatchResult": [payload]}
        )
        publish_catalogue()
        assert dispatch.call_count == 1
        publish_catalogue()
        assert dispatch.call_count == 1
        payload["Status"] = "CANCELLED"
        Importer(season, timezone.now()).apply(
            "club_results", "C", {"MatchResult": [payload]}
        )
        publish_catalogue()
        assert dispatch.call_count == len(["rescheduled", "cancelled"])
        assert dispatch.call_args.kwargs["cancelled"] is True
        source = Match.objects.select_related("local_match__tracker_data").get()
        tracker = source.local_match.tracker_data
        tracker.live_revision = 1
        tracker.save(update_fields=["live_revision"])
        payload["Status"] = "SCHEDULED"
        Importer(season, timezone.now()).apply(
            "club_results", "C", {"MatchResult": [payload]}
        )
        publish_catalogue()
        assert dispatch.call_count == len(["rescheduled", "cancelled"])


@pytest.mark.django_db
def test_notifications_deduplicate_team_and_club_followers(season: Season) -> None:
    """An overlapping follow receives one push; unrelated accounts receive none."""
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [match_payload()]}
    )
    publish_catalogue()
    match = Match.objects.select_related("local_match__home_team").get().local_match
    user = get_user_model().objects.create_user(username="synthetic-follower")
    player = Player.objects.get(user=user)
    player.team_follow.add(match.home_team_id)
    player.club_follow.add(match.home_team.club_id)
    get_user_model().objects.create_user(username="unrelated")
    notification_id = uuid4()
    Match.objects.filter(local_match=match).update(
        schedule_notification_id=notification_id,
        published_schedule={
            "starts_at": match.start_time.isoformat(),
            "status": "CANCELLED",
        },
    )
    send = Mock()
    notify_schedule_change(
        notification_id=str(notification_id),
        match_id=str(match.pk),
        starts_at=match.start_time.isoformat(),
        cancelled=True,
        send_payload=send,
    )
    send.assert_called_once()
    assert send.call_args.kwargs["user_ids"] == [user.pk]
    assert send.call_args.kwargs["payload"].title == "Wedstrijd afgelast"


def test_historical_and_initial_schedule_snapshots_stay_silent() -> None:
    """Only future scheduled/cancelled transitions warrant an alert."""
    before = {
        "starts_at": (timezone.now() - timedelta(days=2)).isoformat(),
        "status": "SCHEDULED",
    }
    after = {
        "starts_at": (timezone.now() - timedelta(days=1)).isoformat(),
        "status": "CANCELLED",
    }
    assert not schedule_changed(before, after)
    assert not schedule_changed({}, after)
    assert not schedule_changed(before, {**after, "status": "FINAL"})


@pytest.mark.django_db
def test_dispatch_waits_for_commit() -> None:
    """Schedule notification intent is atomic with the fixture transaction."""
    with transaction.atomic():
        dispatch_schedule_change(
            notification_id=str(uuid4()),
            match_id="synthetic",
            starts_at="2026-10-01T13:00:00+00:00",
            cancelled=False,
        )
        assert BackgroundJob.objects.count() == 1
        transaction.set_rollback(True)
    assert not BackgroundJob.objects.exists()


@pytest.mark.django_db
def test_superseded_notification_is_not_delivered(season: Season) -> None:
    """A queued cancellation cannot outlive the latest published schedule."""
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [match_payload()]}
    )
    publish_catalogue()
    source = Match.objects.get()
    send = Mock()
    notify_schedule_change(
        notification_id=str(uuid4()),
        match_id=str(source.local_match_id),
        starts_at=source.starts_at.isoformat(),
        cancelled=True,
        send_payload=send,
    )
    send.assert_not_called()


@pytest.mark.django_db
def test_repeated_schedule_state_and_job_redelivery_notify_only_latest_event(
    season: Season,
) -> None:
    """A-B-C-B changes cannot revive B's obsolete queued event or replay delivery."""
    payload = match_payload()
    start = timezone.now() + timedelta(days=5)
    payload.update(Status="SCHEDULED", HomeResult=None, AwayResult=None)
    with patch(
        "apps.competition.services.publishing.schedule_change_dispatcher"
    ) as factory:
        for offset in [0, 1, 2, 1]:
            payload["MatchDateTime"] = (start + timedelta(hours=offset)).isoformat()
            Importer(season, timezone.now()).apply(
                "club_results", "C", {"MatchResult": [payload]}
            )
            publish_catalogue()
    jobs = [call.kwargs for call in factory.return_value.call_args_list]
    assert len(jobs) == len(["B", "C", "B"])
    source = Match.objects.select_related("local_match").get()
    user = get_user_model().objects.create_user(username="repeat-follower")
    Player.objects.get(user=user).team_follow.add(source.local_match.home_team_id)
    send = Mock()
    for job in jobs[:-1]:
        notify_schedule_change(**job, send_payload=send)
    send.assert_not_called()
    original_result = Publisher.result

    def claim_during_publication(
        publisher: Publisher, row: Match, tracker: MatchData, pool_id: UUID | None
    ) -> bool:
        notify_schedule_change(**jobs[-1], send_payload=send)
        return original_result(publisher, row, tracker, pool_id)

    # Publication has already loaded the pending event when the worker claims it.
    Match.objects.filter(pk=source.pk).update(updated_at=timezone.now())
    with patch.object(Publisher, "result", claim_during_publication):
        publish_catalogue()
    notify_schedule_change(**jobs[-1], send_payload=send)
    send.assert_called_once()
    source.refresh_from_db()
    assert source.schedule_notification_id is None
