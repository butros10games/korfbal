"""Celery dispatch adapter for official fixture updates."""

from django.db import transaction

from apps.player.tasks import notify_official_schedule_change


def dispatch_schedule_change(
    *, notification_id: str, match_id: str, starts_at: str, cancelled: bool
) -> None:
    """Publish after commit; a broker outage must not undo imported fixtures."""
    transaction.on_commit(
        lambda: notify_official_schedule_change.delay(
            notification_id=notification_id,
            match_id=match_id,
            starts_at=starts_at,
            cancelled=cancelled,
        ),
        robust=True,
    )
