"""Durable revision tracking for live match state."""

from __future__ import annotations

from collections.abc import Iterable
from functools import partial

from django.db import connection, transaction
from django.utils import timezone

from apps.game_tracker.models import MatchData
from apps.game_tracker.realtime.contracts import ALL_LIVE_RESOURCES, LiveResource
from apps.game_tracker.realtime.publisher import publish_match_changed


def _record_match_change_in_transaction(
    match_data: MatchData,
    *,
    resources: frozenset[LiveResource],
) -> int:
    locked = MatchData.objects.select_for_update().filter(pk=match_data.pk).first()
    if locked is None:
        return match_data.live_revision
    locked.live_revision += 1
    locked.live_changed_at = timezone.now()
    locked.save(update_fields=["live_revision", "live_changed_at"])

    match_data.live_revision = locked.live_revision
    match_data.live_changed_at = locked.live_changed_at

    transaction.on_commit(
        partial(
            publish_match_changed,
            match_id=str(locked.match_link.id_uuid),
            revision=locked.live_revision,
            resources=resources,
        ),
    )
    return locked.live_revision


def record_match_change(
    match_data: MatchData,
    *,
    resources: Iterable[LiveResource] = ALL_LIVE_RESOURCES,
) -> int:
    """Increment a match revision and publish affected resources after commit."""
    normalized = frozenset(resources)
    if not normalized:
        return match_data.live_revision

    if connection.in_atomic_block:
        return _record_match_change_in_transaction(match_data, resources=normalized)

    with transaction.atomic():
        return _record_match_change_in_transaction(match_data, resources=normalized)
