"""Durable dispatch for official fixture updates."""

from apps.kwt_common.services.jobs import enqueue


def dispatch_schedule_change(
    *, notification_id: str, match_id: str, starts_at: str, cancelled: bool
) -> None:
    """Commit notification intent with the imported fixture."""
    enqueue(
        "apps.player.tasks.notify_official_schedule_change",
        notification_id,
        kwargs={
            "notification_id": notification_id,
            "match_id": match_id,
            "starts_at": starts_at,
            "cancelled": cancelled,
        },
        once=True,
    )
