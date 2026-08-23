"""Record append-only envelopes for all typed tracker writes."""

from __future__ import annotations

from django.db.models.signals import post_delete, post_save, pre_delete
from django.dispatch import receiver

from apps.game_tracker.models import (
    Attack,
    MatchData,
    MatchPart,
    Pause,
    PlayerChange,
    Shot,
    Timeout,
)
from apps.game_tracker.services.match_event_context import (
    mark_match_data_deleting,
    match_data_is_deleting,
    unmark_match_data_deleting,
)
from apps.game_tracker.services.match_events import record_typed_match_event


_TRACKED_SENDERS = (Attack, MatchPart, Pause, PlayerChange, Shot, Timeout)


@receiver(pre_delete, sender=MatchData)
def _match_data_delete_started(
    sender: type[MatchData],
    instance: MatchData,
    **kwargs: object,
) -> None:
    del sender, kwargs
    mark_match_data_deleting(instance.pk)


@receiver(post_delete, sender=MatchData)
def _match_data_delete_finished(
    sender: type[MatchData],
    instance: MatchData,
    **kwargs: object,
) -> None:
    del sender, kwargs
    unmark_match_data_deleting(instance.pk)


@receiver(post_save, sender=Attack)
@receiver(post_save, sender=MatchPart)
@receiver(post_save, sender=Pause)
@receiver(post_save, sender=PlayerChange)
@receiver(post_save, sender=Shot)
@receiver(post_save, sender=Timeout)
def _typed_record_saved(
    sender: type[object],
    instance: object,
    created: bool,
    raw: bool = False,
    **kwargs: object,
) -> None:
    del sender, kwargs
    if raw or not isinstance(instance, _TRACKED_SENDERS):
        return
    record_typed_match_event(instance, operation="created" if created else "updated")


@receiver(post_delete, sender=Attack)
@receiver(post_delete, sender=MatchPart)
@receiver(post_delete, sender=Pause)
@receiver(post_delete, sender=PlayerChange)
@receiver(post_delete, sender=Shot)
@receiver(post_delete, sender=Timeout)
def _typed_record_deleted(
    sender: type[object],
    instance: object,
    **kwargs: object,
) -> None:
    del sender, kwargs
    if not isinstance(instance, _TRACKED_SENDERS):
        return
    match_data_id = instance.__dict__.get("match_data_id")
    if match_data_id is not None and match_data_is_deleting(match_data_id):
        return
    record_typed_match_event(instance, operation="deleted")
