"""Mark aggregate deletion cascades so projection side effects stay suppressed."""

from __future__ import annotations

from django.db.models.signals import post_delete, pre_delete
from django.dispatch import receiver

from apps.game_tracker.models import MatchData
from apps.game_tracker.services.match_event_context import (
    mark_match_data_deleting,
    unmark_match_data_deleting,
)


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
