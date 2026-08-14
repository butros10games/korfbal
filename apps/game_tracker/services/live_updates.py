"""Durable revision tracking for live match state."""

from __future__ import annotations

from django.db import connection, transaction
from django.utils import timezone

from apps.game_tracker.models import MatchData


def _record_match_change_in_transaction(
    match_data: MatchData,
) -> int:
    locked = MatchData.objects.select_for_update().filter(pk=match_data.pk).first()
    if locked is None:
        return match_data.live_revision
    locked.live_revision += 1
    locked.live_changed_at = timezone.now()
    locked.save(update_fields=["live_revision", "live_changed_at"])

    match_data.live_revision = locked.live_revision
    match_data.live_changed_at = locked.live_changed_at

    return locked.live_revision


def record_match_change(match_data: MatchData) -> int:
    """Increment and return the durable revision for a committed match change."""
    if connection.in_atomic_block:
        return _record_match_change_in_transaction(match_data)

    with transaction.atomic():
        return _record_match_change_in_transaction(match_data)
