"""Tournament-level durable revision recording."""

from __future__ import annotations

from functools import partial

from django.db import transaction
from django.utils import timezone

from apps.tournament.application.ports import TournamentChangePublisher
from apps.tournament.models import Tournament, TournamentMatch


def _operational_status(tournament: Tournament) -> str:
    """Derive the public lifecycle from the aggregate's match states."""
    if tournament.status in {Tournament.Status.DRAFT, Tournament.Status.ARCHIVED}:
        return tournament.status
    match_statuses = set(tournament.matches.values_list("status", flat=True))
    if not match_statuses:
        return tournament.status
    terminal = {TournamentMatch.Status.FINAL, TournamentMatch.Status.CANCELLED}
    if match_statuses <= terminal:
        return Tournament.Status.FINISHED
    if match_statuses & {TournamentMatch.Status.LIVE, TournamentMatch.Status.FINAL}:
        return Tournament.Status.LIVE
    return Tournament.Status.PUBLISHED


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
        next_status = _operational_status(locked)
        status_changed = next_status != locked.status
        locked.status = next_status
        update_fields = ["live_revision", "live_changed_at", "updated_at"]
        if status_changed:
            update_fields.append("status")
        locked.save(update_fields=update_fields)
        tournament.status = locked.status
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
