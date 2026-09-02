"""Tournament-level durable revision recording."""

from __future__ import annotations

from functools import partial

from django.db import transaction
from django.utils import timezone

from apps.tournament.application.ports import TournamentChangePublisher
from apps.tournament.models import Tournament


def touch_tournament(
    tournament: Tournament,
    *,
    publisher: TournamentChangePublisher,
) -> int:
    """Increment the public snapshot revision and publish after commit."""
    with transaction.atomic():
        locked = Tournament.objects.select_for_update().get(pk=tournament.pk)
        locked.live_revision += 1
        locked.live_changed_at = timezone.now()
        locked.save(update_fields=["live_revision", "live_changed_at", "updated_at"])
        tournament.live_revision = locked.live_revision
        tournament.live_changed_at = locked.live_changed_at
        transaction.on_commit(
            partial(
                publisher.publish,
                tournament_id=str(locked.id_uuid),
                revision=locked.live_revision,
            )
        )
        return locked.live_revision
