"""Production entrypoints bind competition publication to durable job dispatch."""

from datetime import timedelta

from django.utils import timezone
import pytest

from apps.competition import composition
from apps.competition.adapters.outbound.notifications import dispatch_schedule_change
from apps.competition.models import Match
from apps.competition.services.publishing import Publisher
from apps.competition.tests.fakes import RecordingScheduleChanges


def test_schedule_changes_bind_to_the_durable_notification_adapter() -> None:
    """Catalogue publication queues official changes through BackgroundJob intent."""
    assert composition.schedule_change_dispatcher() is dispatch_schedule_change


def rescheduled_fixture() -> Match:
    """Build an unsaved published fixture whose future start time moved."""
    now = timezone.now()
    return Match(
        starts_at=now + timedelta(days=3),
        status="SCHEDULED",
        local_created=True,
        published_schedule={
            "starts_at": (now + timedelta(days=2)).isoformat(),
            "status": "SCHEDULED",
        },
    )


def test_publisher_dispatches_through_the_injected_capability() -> None:
    """A moved fixture reaches exactly the dispatcher the entrypoint supplied."""
    dispatch = RecordingScheduleChanges()
    row = rescheduled_fixture()

    fields = Publisher(dispatch).schedule(row, accepted=True)

    assert fields == ("published_schedule", "schedule_notification_id")
    assert dispatch.calls == [
        {
            "notification_id": str(row.schedule_notification_id),
            "match_id": str(row.local_match_id),
            "starts_at": row.starts_at.isoformat(),
            "cancelled": False,
        }
    ]


def test_repair_publisher_refuses_to_publish_fixture_changes() -> None:
    """A repair-only publisher cannot silently drop an official schedule change."""
    with pytest.raises(RuntimeError, match="schedule dispatcher"):
        Publisher(schedule_changes=None).schedule(rescheduled_fixture(), accepted=True)
